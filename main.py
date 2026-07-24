from fastapi import FastAPI, Request, Query, Path
import typing
import json
import os
import time
import hashlib
import redis
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from Yelp.main import Yelp, YelpError
from Yelp.crawlbase import CrawlbaseError

# --- Redis cache (optional) ---
try:
    redis_client = redis.Redis(
        host=os.getenv("REDIS_HOST", "localhost"),
        port=int(os.getenv("REDIS_PORT", 6379)),
        password=os.getenv("REDIS_PASSWORD", None),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    redis_client.ping()
    CACHE_ENABLED = True
    print("✅ Redis cache connected")
except Exception as e:
    print(f"⚠️  Redis cache disabled: {e}")
    CACHE_ENABLED = False
    redis_client = None

CACHE_TTL = int(os.getenv("CACHE_TTL", 3600))
REVIEW_TTL = int(os.getenv("REVIEW_TTL", 21600))

limiter = Limiter(key_func=get_remote_address, default_limits=["50/second"])

app = FastAPI(
    title="Yelp API (via Crawlbase)",
    description="Unofficial Yelp API — search, business details, reviews, autocomplete. Fetched through Crawlbase.",
    version="3.0.0",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


def get_cache_key(prefix, *parts):
    return f"{prefix}:{hashlib.md5(':'.join(str(p) for p in parts).encode()).hexdigest()}"


def with_cache(cache_key, producer, ttl=CACHE_TTL):
    if CACHE_ENABLED:
        try:
            cached = redis_client.get(cache_key)
            if cached:
                d = json.loads(cached)
                return {**d["data"], "cached": True, "cache_age_seconds": int(time.time() - d.get("cached_at", time.time()))}
        except Exception as e:
            print(f"cache read error: {e}")
    result = producer()
    if CACHE_ENABLED and result.get("status") == "success":
        try:
            redis_client.setex(cache_key, ttl, json.dumps({"data": result, "cached_at": time.time()}))
        except Exception as e:
            print(f"cache write error: {e}")
    return {**result, "cached": False}


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Yelp API (unofficial, via Crawlbase)",
        "endpoints": ["/search", "/business/{id}", "/reviews", "/autocomplete", "/docs"],
    }


@app.get("/debug/search")
async def debug_search(
    term: str = Query("coffee"),
    location: str = Query("San Francisco, CA"),
):
    """Diagnostic: dump the structure of Yelp's search payload so the parser
    can be aligned to the current field names. Remove once parsing is fixed."""
    from Yelp.crawlbase import Crawlbase
    from Yelp.parser import extract_root_props
    from urllib.parse import quote_plus

    url = f"https://www.yelp.com/search?find_desc={quote_plus(term)}&find_loc={quote_plus(location)}"
    html = Crawlbase().get(url)
    root = extract_root_props(html)
    spp = (
        root.get("legacyProps", {})
        .get("searchAppProps", {})
        .get("searchPageProps", {})
    )
    items = spp.get("mainContentComponentsListProps", []) or []

    def keyshape(node, depth=2):
        """Keys of a dict, recursing a couple levels so we see nested field names."""
        if isinstance(node, dict):
            out = {}
            for k, v in list(node.items())[:40]:
                if depth > 0 and isinstance(v, (dict, list)):
                    out[k] = keyshape(v, depth - 1)
                else:
                    out[k] = type(v).__name__
            return out
        if isinstance(node, list):
            return [keyshape(node[0], depth - 1)] if node else []
        return type(node).__name__

    sections = []
    for it in items:
        if isinstance(it, dict) and it.get("type") == "searchResultSection":
            props = it.get("props") or {}
            results = props.get("searchResults") or []
            entry = {
                "sectionId": props.get("sectionId"),
                "isAdOnly": props.get("isAdOnly"),
                "results_count": len(results),
            }
            if results:
                # full nested shape of the first business result
                entry["first_result_shape"] = keyshape(results[0], depth=3)
            sections.append(entry)

    return {
        "html_len": len(html),
        "sections": sections,
    }


@app.get("/search")
@limiter.limit("50/second")
async def search_businesses(
    request: Request,
    term: str = Query(..., description="Search term, e.g. 'coffee'"),
    location: str = Query(..., description="Location, e.g. 'San Francisco, CA'"),
    offset: int = Query(0, ge=0, description="Result offset (page size 10)"),
    limit: int = Query(10, ge=1, le=10),
    sort_by: typing.Optional[str] = Query(None, description="recommended | rating | review_count | distance"),
    price: typing.Optional[str] = Query(None, description="1..4 or CSV e.g. '1,2'"),
):
    """Search Yelp businesses by term + location."""
    try:
        key = get_cache_key("yelp_search", term, location, offset, limit, sort_by, price)

        def produce():
            r = Yelp().search(term, location, offset=offset, limit=limit, sort_by=sort_by, price=price)
            return {
                "status": "success",
                "term": term,
                "location": location,
                "offset": offset,
                "total_results": r.get("total_results"),
                "results_count": len(r["businesses"]),
                "search_url": r.get("search_url"),
                "businesses": r["businesses"],
            }

        return with_cache(key, produce)
    except (YelpError, CrawlbaseError) as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/business/{id}")
@limiter.limit("50/second")
async def business_details(
    request: Request,
    id: str = Path(..., description="Business alias (URL slug) or encoded biz id"),
):
    """Full business detail: hours, phone, website, categories, photos."""
    try:
        key = get_cache_key("yelp_biz", id)

        def produce():
            return {"status": "success", "business": Yelp().business(id)}

        return with_cache(key, produce)
    except (YelpError, CrawlbaseError) as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/reviews")
@limiter.limit("50/second")
async def business_reviews(
    request: Request,
    business_id: str = Query(..., description="Encoded biz id (from /search) or alias"),
    offset: int = Query(0, ge=0),
    limit: int = Query(10, ge=1, le=50),
    sort_by: str = Query("DATE_DESC", description="DATE_DESC | DATE_ASC | RATING_DESC | RATING_ASC | ELITES_DESC"),
    language: str = Query("en"),
):
    """Paginated reviews for a business."""
    try:
        key = get_cache_key("yelp_reviews", business_id, offset, limit, sort_by, language)

        def produce():
            r = Yelp().reviews(business_id, offset=offset, limit=limit, sort_by=sort_by, language=language)
            return {
                "status": "success",
                "business_id": r.get("business_id"),
                "offset": offset,
                "total_results": r.get("total_results"),
                "results_count": len(r["reviews"]),
                "reviews": r["reviews"],
            }

        return with_cache(key, produce, ttl=REVIEW_TTL)
    except (YelpError, CrawlbaseError) as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/autocomplete")
@limiter.limit("50/second")
async def autocomplete(
    request: Request,
    prefix: str = Query(..., description="Typed prefix, e.g. 'coff'"),
    location: str = Query("", description="Optional location context"),
):
    """Search suggestions (terms, businesses, categories) for a prefix."""
    try:
        key = get_cache_key("yelp_ac", prefix, location)

        def produce():
            r = Yelp().autocomplete(prefix, location=location)
            return {
                "status": "success",
                "prefix": prefix,
                "location": location,
                "terms": r.get("terms", []),
                "businesses": r.get("businesses", []),
                "categories": r.get("categories", []),
            }

        return with_cache(key, produce)
    except (YelpError, CrawlbaseError) as e:
        return {"status": "error", "message": str(e)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
