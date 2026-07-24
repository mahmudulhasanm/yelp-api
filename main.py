from fastapi import FastAPI, HTTPException, Query, Request, Path
from Yelp.main import Yelp, YelpError
import typing
import redis
import json
import hashlib
import os
import time
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

# Initialize Redis cache
try:
    redis_client = redis.Redis(
        host=os.getenv('REDIS_HOST', 'localhost'),
        port=int(os.getenv('REDIS_PORT', 6379)),
        password=os.getenv('REDIS_PASSWORD', None),
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2
    )
    redis_client.ping()
    CACHE_ENABLED = True
    print("✅ Redis cache connected")
except Exception as e:
    print(f"⚠️  Redis cache disabled: {e}")
    CACHE_ENABLED = False
    redis_client = None

# Yelp pages change less often than flight prices; cache longer by default.
CACHE_TTL = int(os.getenv('CACHE_TTL', 3600))       # 1 hour
REVIEW_TTL = int(os.getenv('REVIEW_TTL', 21600))    # 6 hours
PROXY = os.getenv('PROXY') or None

limiter = Limiter(key_func=get_remote_address, default_limits=["50/second"])

app = FastAPI(
    title="Yelp API",
    description="Unofficial Yelp scraper API — search, business details, reviews, autocomplete.",
    version="1.0.0",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


def get_cache_key(prefix: str, *parts) -> str:
    key_data = ":".join(str(p) for p in parts)
    return f"{prefix}:{hashlib.md5(key_data.encode()).hexdigest()}"


def get_from_cache(cache_key: str):
    if not CACHE_ENABLED:
        return None
    try:
        cached = redis_client.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        print(f"Cache read error: {e}")
    return None


def set_to_cache(cache_key: str, data: dict, ttl: int = CACHE_TTL):
    if not CACHE_ENABLED:
        return
    try:
        redis_client.setex(cache_key, ttl, json.dumps(data))
    except Exception as e:
        print(f"Cache write error: {e}")


def with_cache(cache_key: str, producer, ttl: int = CACHE_TTL):
    """Cache wrapper: returns cached result or runs producer and caches it."""
    cached_data = get_from_cache(cache_key)
    if cached_data:
        cache_age = time.time() - cached_data.get('cached_at', time.time())
        return {**cached_data['data'], "cached": True, "cache_age_seconds": int(cache_age)}

    result = producer()
    set_to_cache(cache_key, {'data': result, 'cached_at': time.time()}, ttl)
    return {**result, "cached": False}


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Yelp API (unofficial)",
        "endpoints": ["/search", "/business/{id}", "/reviews", "/autocomplete", "/docs"],
    }


@app.get("/debug/ip")
async def debug_ip():
    """Diagnostic: shows the outbound IP/country the app uses.

    If PROXY is set correctly, this reflects the proxy's exit IP. If it shows
    the server's own IP (or proxy_configured is false), the PROXY env var isn't
    taking effect. Remove this endpoint once diagnosis is done.
    """
    from Yelp.main import Yelp
    y = Yelp(proxy=PROXY)
    info = {"proxy_configured": bool(PROXY)}
    try:
        # Bright Data's own test endpoint returns the exit IP + geo.
        info["brdtest"] = y._get("https://geo.brdtest.com/welcome.txt?product=dc&method=native")
    except Exception as e:
        info["brdtest_error"] = str(e)
    try:
        info["ipify"] = y._get("https://api.ipify.org?format=json")
    except Exception as e:
        info["ipify_error"] = str(e)
    return info


@app.get("/search")
@limiter.limit("50/second")
async def search_businesses(
    request: Request,
    term: str = Query(..., description="Search term, e.g. 'coffee', 'plumbers', 'sushi'"),
    location: str = Query(..., description="Location, e.g. 'San Francisco, CA'"),
    offset: int = Query(0, ge=0, description="Result offset (page size is 10)"),
    limit: int = Query(10, ge=1, le=10, description="Max results to return (<=10 per page)"),
    sort_by: typing.Optional[str] = Query(None, description="recommended | rating | review_count | distance"),
    price: typing.Optional[str] = Query(None, description="Price filter: 1..4 or CSV e.g. '1,2' ($..$$$$)"),
):
    """Search Yelp businesses by term + location."""
    try:
        cache_key = get_cache_key("yelp_search", term, location, offset, limit, sort_by, price)

        def produce():
            y = Yelp(proxy=PROXY)
            result = y.search(term, location, offset=offset, limit=limit, sort_by=sort_by, price=price)
            return {
                "status": "success",
                "term": term,
                "location": location,
                "offset": offset,
                "total_results": result.get("total_results"),
                "results_count": len(result["businesses"]),
                "search_url": result.get("search_url"),
                "businesses": result["businesses"],
            }

        return with_cache(cache_key, produce)
    except YelpError as ye:
        return {"status": "error", "message": str(ye)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/business/{id}")
@limiter.limit("50/second")
async def business_details(
    request: Request,
    id: str = Path(..., description="Business alias (URL slug) or encoded biz id"),
):
    """Full business detail: hours, phone, website, categories, photos, attributes."""
    try:
        cache_key = get_cache_key("yelp_biz", id)

        def produce():
            y = Yelp(proxy=PROXY)
            data = y.business(id)
            return {"status": "success", "business": data}

        return with_cache(cache_key, produce)
    except YelpError as ye:
        return {"status": "error", "message": str(ye)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/reviews")
@limiter.limit("50/second")
async def business_reviews(
    request: Request,
    business_id: str = Query(..., description="Encoded biz id (from /search) or business alias"),
    offset: int = Query(0, ge=0, description="Review offset"),
    limit: int = Query(10, ge=1, le=50, description="Reviews per page (<=50)"),
    sort_by: str = Query("DATE_DESC", description="DATE_DESC | DATE_ASC | RATING_DESC | RATING_ASC | ELITES_DESC"),
    language: str = Query("en", description="Language code, e.g. 'en'"),
):
    """Paginated reviews for a business."""
    try:
        cache_key = get_cache_key("yelp_reviews", business_id, offset, limit, sort_by, language)

        def produce():
            y = Yelp(proxy=PROXY)
            result = y.reviews(business_id, offset=offset, limit=limit, sort_by=sort_by, language=language)
            return {
                "status": "success",
                "business_id": result.get("business_id"),
                "offset": offset,
                "total_results": result.get("total_results"),
                "results_count": len(result["reviews"]),
                "reviews": result["reviews"],
            }

        return with_cache(cache_key, produce, ttl=REVIEW_TTL)
    except YelpError as ye:
        return {"status": "error", "message": str(ye)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/autocomplete")
@limiter.limit("50/second")
async def autocomplete(
    request: Request,
    prefix: str = Query(..., description="Typed prefix, e.g. 'coff'"),
    location: str = Query("", description="Optional location context, e.g. 'San Francisco, CA'"),
):
    """Search suggestions (terms, businesses, categories) for a typed prefix."""
    try:
        cache_key = get_cache_key("yelp_ac", prefix, location)

        def produce():
            y = Yelp(proxy=PROXY)
            result = y.autocomplete(prefix, location=location)
            return {
                "status": "success",
                "prefix": prefix,
                "location": location,
                "terms": result.get("terms", []),
                "businesses": result.get("businesses", []),
                "categories": result.get("categories", []),
            }

        return with_cache(cache_key, produce)
    except YelpError as ye:
        return {"status": "error", "message": str(ye)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
