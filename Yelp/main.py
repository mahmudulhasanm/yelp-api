"""
Yelp scraper — same reverse-engineering method as the Google Flights / Ads
Transparency scrapers: hit Yelp's own internal endpoints directly with a
Chrome-impersonated TLS session (primp), no headless browser.

Endpoints used (all undocumented, meant for Yelp's own frontend):

1. Search   GET  https://www.yelp.com/search/snippet?find_desc=&find_loc=&start=
            -> HTML page with a `react_root_props` JSON blob we parse.

2. Business GET  https://www.yelp.com/biz/{alias}
            -> HTML page; encoded biz id from <meta name="yelp-biz-id">, detail
               from the same `react_root_props` blob.

3. Reviews  POST https://www.yelp.com/gql/batch
            -> GraphQL "GetBusinessReviewFeed" operation, paginated via a
               base64-encoded `after` offset cursor.

4. Autocomplete GET https://www.yelp.com/search_suggest/v2/prefetch?prefix=&loc=
            -> JSON suggestion groups (terms / businesses / categories).

Yelp blocks aggressively. For any real volume, pass a residential PROXY and keep
per-IP rate low. The reviews GraphQL `documentId` is a persisted-query hash that
Yelp rotates occasionally; override it via YELP_REVIEW_DOC_ID when it drifts.
"""

import base64
import json
import os
from typing import List, Optional

from primp import Client as PrimpClient

from Yelp.parser import (
    parse_search,
    parse_business,
    parse_reviews,
    parse_autocomplete,
    extract_biz_id,
)

BASE = "https://www.yelp.com"

# Persisted-query hash for GetBusinessReviewFeed. Yelp rotates this; override
# with the env var when reviews stop returning.
DEFAULT_REVIEW_DOC_ID = "ef51f33d1b0eccc958dddbf6cde15739c48b34637a00ebe316441031d4bf7681"

IMPERSONATE = "chrome_133"


class YelpError(Exception):
    pass


class Yelp:
    def __init__(self, proxy: Optional[str] = None, timeout: int = 25):
        self.proxy = proxy or os.getenv("PROXY") or None
        self.timeout = timeout
        self.review_doc_id = os.getenv("YELP_REVIEW_DOC_ID", DEFAULT_REVIEW_DOC_ID)

    # -- session -----------------------------------------------------------
    def _client(self) -> PrimpClient:
        kwargs = dict(impersonate=IMPERSONATE, timeout=self.timeout)
        if self.proxy:
            kwargs["proxy"] = self.proxy
        try:
            return PrimpClient(**kwargs)
        except Exception:
            # Older primp builds name the arg differently / lack the profile.
            kwargs.pop("impersonate", None)
            return PrimpClient(**kwargs)

    def _get(self, url: str, params: dict = None, headers: dict = None) -> str:
        c = self._client()
        r = c.get(url, params=params, headers=self._headers(headers))
        if r.status_code != 200:
            raise YelpError(f"GET {url} -> HTTP {r.status_code} (likely blocked; try a proxy)")
        return r.text

    def _post(self, url: str, content: str, headers: dict = None) -> str:
        c = self._client()
        r = c.post(url, content=content, headers=self._headers(headers))
        if r.status_code != 200:
            raise YelpError(f"POST {url} -> HTTP {r.status_code} (likely blocked; try a proxy)")
        return r.text

    @staticmethod
    def _headers(extra: dict = None) -> dict:
        h = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "accept-language": "en-US,en;q=0.9",
            "cache-control": "no-cache",
            # pre-answers the interstitial that otherwise blocks datacenter IPs
            "cookie": "intl_splash=false; wdi=1",
        }
        if extra:
            h.update(extra)
        return h

    # -- 1. search ---------------------------------------------------------
    def search(
        self,
        term: str,
        location: str,
        offset: int = 0,
        limit: int = 10,
        sort_by: Optional[str] = None,
        price: Optional[str] = None,
    ) -> dict:
        """Search businesses by term + location.

        offset  -> Yelp `start` (page is 10 results)
        sort_by -> recommended | rating | review_count | distance (best effort)
        price   -> "1", "2", "1,2", ... ($ .. $$$$)
        """
        params = {
            "find_desc": term,
            "find_loc": location,
            "start": offset,
            "ns": 1,
            "request_origin": "user",
        }
        if sort_by:
            params["sortby"] = sort_by
        if price:
            params["attrs"] = "RestaurantsPriceRange2." + price
        html = self._get(f"{BASE}/search/snippet", params=params)
        result = parse_search(html)
        result["businesses"] = result["businesses"][:limit]
        result["term"] = term
        result["location"] = location
        result["offset"] = offset
        result["search_url"] = (
            f"{BASE}/search?find_desc={term.replace(' ', '+')}"
            f"&find_loc={location.replace(' ', '+')}"
        )
        return result

    # -- 2. business detail ------------------------------------------------
    def business(self, id_or_alias: str) -> dict:
        """Business detail by alias (URL slug) or by encoded biz id.

        Yelp detail pages are keyed by alias (e.g. `vons-1000-spirits-seattle-4`).
        An encoded id (e.g. `Lw7NmZ3j-WEye97ywEmkXQ`) also resolves via /biz/{id}.
        """
        alias = id_or_alias.strip().strip("/")
        if alias.startswith("http"):
            url = alias
        elif "/biz/" in alias:
            url = f"{BASE}/biz/{alias.split('/biz/')[-1].lstrip('/')}"
        else:
            url = f"{BASE}/biz/{alias}"
        html = self._get(url)
        data = parse_business(html)
        if not data.get("alias"):
            data["alias"] = alias
        data["url"] = url
        return data

    # -- 3. reviews --------------------------------------------------------
    def reviews(
        self,
        biz_id: str,
        offset: int = 0,
        limit: int = 10,
        sort_by: str = "DATE_DESC",
        language: str = "en",
    ) -> dict:
        """Reviews for a business via the GraphQL feed.

        biz_id must be the ENCODED business id (from search results `id` or the
        <meta name="yelp-biz-id"> on the detail page). If an alias is passed we
        resolve the encoded id first.
        """
        enc_id = biz_id
        alias_for_referer = biz_id
        if not _looks_encoded(biz_id):
            html = self._get(f"{BASE}/biz/{biz_id.strip('/')}")
            resolved = extract_biz_id(html)
            if not resolved:
                raise YelpError(f"Could not resolve encoded biz id for '{biz_id}'")
            enc_id = resolved
            alias_for_referer = biz_id.strip("/")

        after = base64.b64encode(
            json.dumps({"version": 1, "type": "offset", "offset": offset}).encode()
        ).decode()

        payload = json.dumps([{
            "operationName": "GetBusinessReviewFeed",
            "variables": {
                "encBizId": enc_id,
                "reviewsPerPage": min(limit, 50),
                "selectedReviewEncId": "",
                "hasSelectedReview": False,
                "sortBy": sort_by,
                "languageCode": language,
                "ratings": [5, 4, 3, 2, 1],
                "isSearching": False,
                "after": after,
                "isTranslating": False,
                "translateLanguageCode": language,
                "reactionsSourceFlow": "businessPageReviewSection",
                "minConfidenceLevel": "HIGH_CONFIDENCE",
                "highlightType": "",
                "highlightIdentifier": "",
                "isHighlighting": False,
            },
            "extensions": {
                "operationType": "query",
                "documentId": self.review_doc_id,
            },
        }])

        referer = (
            alias_for_referer if str(alias_for_referer).startswith("http")
            else f"{BASE}/biz/{alias_for_referer}"
        )
        headers = {
            "accept": "*/*",
            "content-type": "application/json",
            "origin": BASE,
            "referer": referer,
            "x-apollo-operation-name": "GetBusinessReviewFeed",
        }
        raw = self._post(f"{BASE}/gql/batch", content=payload, headers=headers)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise YelpError("Reviews response was not JSON (documentId may be stale)")
        result = parse_reviews(data)
        result["business_id"] = enc_id
        result["offset"] = offset
        result["reviews"] = result["reviews"][:limit]
        return result

    # -- 4. autocomplete ---------------------------------------------------
    def autocomplete(self, prefix: str, location: str = "") -> dict:
        """Term / business / category suggestions for a typed prefix."""
        params = {"prefix": prefix, "loc": location}
        raw = self._get(
            f"{BASE}/search_suggest/v2/prefetch",
            params=params,
            headers={"accept": "application/json, text/javascript, */*; q=0.01",
                     "x-requested-with": "XMLHttpRequest"},
        )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = raw
        result = parse_autocomplete(data)
        result["prefix"] = prefix
        result["location"] = location
        return result


def _looks_encoded(s: str) -> bool:
    """Distinguish an encoded biz id from a URL alias.

    Encoded ids are ~22-char url-safe base64 that mix upper/lower case and
    digits with at most a couple of -/_ separators (e.g. `Lw7NmZ3j-WEye97ywEmkXQ`).
    Aliases are always lowercase slugs with several hyphens
    (e.g. `vons-1000-spirits-seattle-4`).
    """
    s = s.strip()
    if "/" in s or " " in s or not (18 <= len(s) <= 30):
        return False
    if not all(c.isalnum() or c in "-_" for c in s):
        return False
    if (s.count("-") + s.count("_")) > 2:
        return False  # multi-word alias
    # Encoded ids almost always contain an uppercase letter; aliases never do.
    return any(c.isupper() for c in s)
