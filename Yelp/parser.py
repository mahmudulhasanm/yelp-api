"""
Yelp HTML/JSON parsers.

Yelp embeds a big JSON blob in every page inside:
    <script data-id="react-root-props">react_root_props = {...};</script>

Search results, business detail and location context all come out of that blob,
so parsing is: pull the script, json.loads it, then dig for the fields we want.
Parsing is defensive (dict.get chains, regex fallbacks) because Yelp reshapes
this payload frequently.
"""

import json
import re
from typing import Optional


# ---------------------------------------------------------------------------
# react_root_props extraction
# ---------------------------------------------------------------------------

_ROOT_RE = re.compile(
    r'react_root_props\s*=\s*(\{.*?\})\s*;?\s*</script>',
    re.DOTALL,
)
_ROOT_RE_ALT = re.compile(
    r'data-id="react-root-props"[^>]*>\s*react_root_props\s*=\s*(\{.*?\});',
    re.DOTALL,
)


def extract_root_props(html: str) -> dict:
    """Return the parsed `react_root_props` JSON object, or {} if not found."""
    for rx in (_ROOT_RE_ALT, _ROOT_RE):
        m = rx.search(html)
        if m:
            blob = m.group(1)
            try:
                return json.loads(blob)
            except json.JSONDecodeError:
                # Greedy match may have swallowed a trailing `;</script>`; trim.
                blob = blob.rsplit(";", 1)[0]
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    continue
    return {}


def extract_biz_id(html: str) -> Optional[str]:
    """Encoded business id lives in <meta name="yelp-biz-id" content="...">."""
    m = re.search(r'<meta\s+name="yelp-biz-id"\s+content="([^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'"bizId"\s*:\s*"([^"]+)"', html)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _search_page_props(root: dict) -> dict:
    return (
        root.get("legacyProps", {})
        .get("searchAppProps", {})
        .get("searchPageProps", {})
    )


def parse_search(html: str) -> dict:
    """Parse the /search/snippet page into a normalised list of businesses."""
    root = extract_root_props(html)
    spp = _search_page_props(root)
    total = spp.get("searchContext", {}).get("totalResults", 0)

    businesses = []
    for item in spp.get("mainContentComponentsListProps", []):
        biz_id = item.get("bizId")
        if not biz_id:
            continue  # ad slots / separators / cta cards have no bizId
        businesses.append(_normalise_search_item(item))

    return {"total_results": total, "businesses": businesses}


def _normalise_search_item(item: str) -> dict:
    """Flatten one search card into a stable, documented shape."""
    biz = item.get("searchResultBusiness", {}) or item

    def rating():
        r = biz.get("rating")
        if isinstance(r, dict):
            return r.get("value") or r.get("rating")
        return r

    categories = [
        c.get("title")
        for c in (biz.get("categories") or [])
        if isinstance(c, dict) and c.get("title")
    ]

    location = biz.get("formattedAddress")
    if not location:
        parts = biz.get("neighborhoods") or []
        location = ", ".join(p for p in parts if p) or None

    coords = biz.get("mapMarker") or biz.get("coordinates") or {}
    photos = [p.get("src") for p in (biz.get("photos") or []) if isinstance(p, dict) and p.get("src")]

    return {
        "id": item.get("bizId"),
        "alias": biz.get("alias"),
        "name": biz.get("name"),
        "url": _abs_url(biz.get("businessUrl") or biz.get("website")),
        "rating": rating(),
        "review_count": biz.get("reviewCount"),
        "price": biz.get("priceRange") or biz.get("price"),
        "categories": categories,
        "phone": biz.get("phone"),
        "address": location,
        "neighborhoods": biz.get("neighborhoods") or [],
        "latitude": coords.get("latitude"),
        "longitude": coords.get("longitude"),
        "is_ad": bool(item.get("adLoggingInfo") or biz.get("isAd")),
        "photo": photos[0] if photos else None,
        "photos": photos,
        "snippet": (biz.get("snippet") or {}).get("text") if isinstance(biz.get("snippet"), dict) else biz.get("snippet"),
        "service_area_business": biz.get("serviceAreaBusiness"),
    }


# ---------------------------------------------------------------------------
# Business detail
# ---------------------------------------------------------------------------

_DAY_ORDER = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def parse_business(html: str) -> dict:
    """Parse a /biz/{alias} page into a business-detail object."""
    root = extract_root_props(html)
    biz_id = extract_biz_id(html)

    # The biz page keeps structured data under bizDetailsPageProps in newer
    # layouts; fall back to regex scraping of the visible page otherwise.
    props = (
        root.get("legacyProps", {})
        .get("bizDetailsProps", {})
        or root.get("bizDetailsPageProps", {})
        or {}
    )
    bizatts = props.get("bizContactInfoProps", {}) or {}

    name = props.get("businessName") or _first_group(html, r'<h1[^>]*>([^<]+)</h1>')
    website = bizatts.get("businessWebsite", {}).get("linkText") if isinstance(bizatts.get("businessWebsite"), dict) else None
    phone = bizatts.get("phoneNumber", {}).get("text") if isinstance(bizatts.get("phoneNumber"), dict) else None
    address = bizatts.get("address") or _first_group(html, r'Get Directions[^<]*</a></p><p[^>]*>([^<]+)</p>')

    rating = props.get("rating") or props.get("aggregateRating", {}).get("ratingValue") if isinstance(props.get("aggregateRating"), dict) else props.get("rating")
    review_count = props.get("reviewCount") or (props.get("aggregateRating", {}) or {}).get("reviewCount")

    categories = [
        c.get("title") if isinstance(c, dict) else c
        for c in (props.get("categories") or [])
    ]
    categories = [c for c in categories if c]

    hours = _parse_hours(html, props)
    photos = _parse_biz_photos(props)
    coords = props.get("coordinates") or props.get("mapMarker") or {}

    return {
        "id": biz_id,
        "alias": props.get("alias"),
        "name": name,
        "url": _abs_url(props.get("businessUrl")),
        "rating": rating,
        "review_count": review_count,
        "price": props.get("priceRange") or props.get("price"),
        "categories": categories,
        "phone": phone,
        "website": website,
        "address": address.strip() if isinstance(address, str) else address,
        "latitude": coords.get("latitude"),
        "longitude": coords.get("longitude"),
        "hours": hours,
        "is_claimed": props.get("isClaimed"),
        "is_open_now": props.get("isOpenNow"),
        "photos": photos,
        "photo_count": props.get("photoCount"),
        "attributes": props.get("organizedProperties") or props.get("attributes") or [],
    }


def _parse_hours(html: str, props: dict) -> dict:
    # Prefer structured hours if present.
    for key in ("hoursInfoRows", "bizHoursProps", "regularHours"):
        val = props.get(key)
        if val:
            out = {}
            rows = val if isinstance(val, list) else val.get("hoursInfoRows", [])
            for row in rows or []:
                day = (row.get("day") or row.get("dayOfWeek") or "").strip().lower()[:3]
                times = row.get("hours") or row.get("timeRanges") or row.get("value")
                if isinstance(times, list):
                    times = ", ".join(
                        t.get("value") if isinstance(t, dict) else str(t) for t in times
                    )
                if day:
                    out[day] = times
            if out:
                return out

    # Fallback: scrape the visible hours table.
    out = {}
    for m in re.finditer(
        r'day-of-the-week[^>]*>\s*([A-Za-z]{3})[^<]*</p>.*?<p[^>]*>([^<]+)</p>',
        html,
        re.DOTALL,
    ):
        out[m.group(1).strip().lower()] = m.group(2).strip()
    return out


def _parse_biz_photos(props: dict) -> list:
    photos = []
    for p in props.get("photoHeaderProps", {}).get("photos", []) if isinstance(props.get("photoHeaderProps"), dict) else []:
        src = p.get("src") or p.get("photoUrl")
        if src:
            photos.append(src)
    return photos


# ---------------------------------------------------------------------------
# Reviews (GraphQL response)
# ---------------------------------------------------------------------------

def parse_reviews(payload) -> dict:
    """Parse the /gql/batch GetBusinessReviewFeed response."""
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    data = (payload or {}).get("data", {})
    feed = data.get("business", {}).get("reviews") or data.get("reviews") or {}

    edges = feed.get("edges") or []
    total = feed.get("totalCount") or feed.get("count")

    reviews = []
    for edge in edges:
        node = edge.get("node", edge) if isinstance(edge, dict) else {}
        author = node.get("author") or {}

        text = node.get("text")
        if isinstance(text, dict):
            text = text.get("full") or text.get("plain")

        created = node.get("createdAt")
        date = node.get("localizedDate")
        if not date:
            date = created.get("localDateTimeForBusiness") if isinstance(created, dict) else created

        reviews.append({
            "id": node.get("encid") or node.get("id"),
            "rating": node.get("rating"),
            "text": text,
            "date": date,
            "author_name": author.get("displayName") or author.get("markupDisplayName"),
            "author_location": (author.get("displayLocation") or None),
            "author_review_count": author.get("reviewCount"),
            "feedback": {
                "useful": (node.get("feedback") or {}).get("useful"),
                "funny": (node.get("feedback") or {}).get("funny"),
                "cool": (node.get("feedback") or {}).get("cool"),
            } if node.get("feedback") else None,
            "photos": [
                p.get("src") for p in (node.get("photos") or [])
                if isinstance(p, dict) and p.get("src")
            ],
        })

    return {"total_results": total, "reviews": reviews}


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------

def parse_autocomplete(payload) -> dict:
    """Parse the /search_suggest/v2/prefetch response."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {"terms": [], "businesses": [], "categories": []}

    response = payload.get("response") if isinstance(payload, dict) else None
    groups = response if isinstance(response, list) else (payload if isinstance(payload, list) else [])

    terms, businesses, categories = [], [], []
    for group in groups:
        for s in (group.get("suggestions") or []):
            title = s.get("title") or s.get("query") or s.get("text")
            stype = (s.get("suggestionType") or s.get("type") or "").lower()
            if not title:
                continue
            if "biz" in stype or s.get("redirectUrl", "").startswith("/biz"):
                businesses.append({"name": title, "url": _abs_url(s.get("redirectUrl"))})
            elif "categor" in stype:
                categories.append(title)
            else:
                terms.append(title)

    return {"terms": terms, "businesses": businesses, "categories": categories}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _abs_url(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    if path.startswith("http"):
        return path
    return "https://www.yelp.com" + path


def _first_group(text: str, pattern: str) -> Optional[str]:
    m = re.search(pattern, text, re.DOTALL)
    return m.group(1).strip() if m else None
