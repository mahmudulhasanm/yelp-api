"""
Yelp scraper — fetches Yelp's own pages/endpoints through the Crawlbase
Crawling API (which handles proxies + anti-bot), then parses them with the
parsers in Yelp/parser.py.

Endpoints hit on Yelp (all via Crawlbase):
    search       GET  /search?find_desc=&find_loc=      -> react_root_props JSON
    business     GET  /biz/{alias}                        -> react_root_props JSON + meta biz-id
    reviews      POST /gql/batch (GetBusinessReviewFeed)  -> GraphQL JSON
    autocomplete GET  /search_suggest/v2/prefetch         -> suggestions JSON
"""

import base64
import json
import os
from typing import Optional
from urllib.parse import quote_plus

from Yelp.crawlbase import Crawlbase, CrawlbaseError
from Yelp.parser import (
    parse_search,
    parse_business,
    parse_reviews,
    parse_autocomplete,
    extract_biz_id,
)

BASE = "https://www.yelp.com"

DEFAULT_REVIEW_DOC_ID = "ef51f33d1b0eccc958dddbf6cde15739c48b34637a00ebe316441031d4bf7681"


class YelpError(Exception):
    pass


def _looks_encoded(s: str) -> bool:
    """Encoded biz id (mixed-case ~22-char base64) vs lowercase URL alias."""
    s = s.strip()
    if "/" in s or " " in s or not (18 <= len(s) <= 30):
        return False
    if not all(c.isalnum() or c in "-_" for c in s):
        return False
    if (s.count("-") + s.count("_")) > 2:
        return False
    return any(c.isupper() for c in s)


class Yelp:
    def __init__(self):
        self.cb = Crawlbase()
        self.review_doc_id = os.getenv("YELP_REVIEW_DOC_ID", DEFAULT_REVIEW_DOC_ID)

    # -- 1. search ---------------------------------------------------------
    def search(self, term, location, offset=0, limit=10, sort_by=None, price=None):
        url = f"{BASE}/search?find_desc={quote_plus(term)}&find_loc={quote_plus(location)}"
        if offset:
            url += f"&start={offset}"
        if sort_by:
            url += f"&sortby={sort_by}"
        if price:
            url += f"&attrs=RestaurantsPriceRange2.{price}"

        html = self.cb.get(url)
        result = parse_search(html)
        result["businesses"] = result["businesses"][:limit]
        result["term"] = term
        result["location"] = location
        result["offset"] = offset
        result["search_url"] = url
        return result

    # -- 2. business detail ------------------------------------------------
    def business(self, id_or_alias: str) -> dict:
        alias = id_or_alias.strip().strip("/")
        if alias.startswith("http"):
            url = alias
        elif "/biz/" in alias:
            url = f"{BASE}/biz/{alias.split('/biz/')[-1].lstrip('/')}"
        else:
            url = f"{BASE}/biz/{alias}"
        html = self.cb.get(url)
        data = parse_business(html)
        if not data.get("alias"):
            data["alias"] = alias
        data["url"] = url
        return data

    # -- 3. reviews --------------------------------------------------------
    def reviews(self, biz_id, offset=0, limit=10, sort_by="DATE_DESC", language="en"):
        enc_id = biz_id
        alias_for_referer = biz_id
        if not _looks_encoded(biz_id):
            html = self.cb.get(f"{BASE}/biz/{biz_id.strip('/')}")
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

        raw = self.cb.post(f"{BASE}/gql/batch", body=payload, content_type="json")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            raise YelpError("Reviews response was not JSON (documentId may be stale, or blocked)")
        result = parse_reviews(data)
        result["business_id"] = enc_id
        result["offset"] = offset
        result["reviews"] = result["reviews"][:limit]
        return result

    # -- 4. autocomplete ---------------------------------------------------
    def autocomplete(self, prefix: str, location: str = "") -> dict:
        url = (
            f"{BASE}/search_suggest/v2/prefetch"
            f"?prefix={quote_plus(prefix)}&loc={quote_plus(location)}"
        )
        raw = self.cb.get(url)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = raw
        result = parse_autocomplete(data)
        result["prefix"] = prefix
        result["location"] = location
        return result
