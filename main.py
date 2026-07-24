from fastapi import FastAPI, Request, Body, Path, Query
import json
import os
import time
import redis
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from Yelp.brightdata import BrightDataYelp, BrightDataError, alias_to_url

# ---------------------------------------------------------------------------
# Redis: used here as a small job registry (job_id -> {type, input, created})
# and to cache finished results. Optional — the API still works without it,
# it just can't label a job's type on /result.
# ---------------------------------------------------------------------------
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
    print("✅ Redis connected")
except Exception as e:
    print(f"⚠️  Redis disabled: {e}")
    CACHE_ENABLED = False
    redis_client = None

RESULT_TTL = int(os.getenv("RESULT_TTL", 86400))  # keep finished results 24h
JOB_TTL = int(os.getenv("JOB_TTL", 86400))

limiter = Limiter(key_func=get_remote_address, default_limits=["50/second"])

app = FastAPI(
    title="Yelp API (Bright Data)",
    description=(
        "Yelp business & review data via Bright Data's prebuilt Yelp scrapers. "
        "Asynchronous job model: POST to trigger a collection, then poll GET /result/{job_id}."
    ),
    version="2.0.0",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


def _save_job(job_id: str, meta: dict):
    if CACHE_ENABLED:
        try:
            redis_client.setex(f"job:{job_id}", JOB_TTL, json.dumps(meta))
        except Exception as e:
            print(f"job save error: {e}")


def _load_job(job_id: str) -> dict:
    if CACHE_ENABLED:
        try:
            v = redis_client.get(f"job:{job_id}")
            if v:
                return json.loads(v)
        except Exception as e:
            print(f"job load error: {e}")
    return {}


def _cache_result(job_id: str, data: dict):
    if CACHE_ENABLED:
        try:
            redis_client.setex(f"result:{job_id}", RESULT_TTL, json.dumps(data))
        except Exception as e:
            print(f"result cache error: {e}")


def _cached_result(job_id: str):
    if CACHE_ENABLED:
        try:
            v = redis_client.get(f"result:{job_id}")
            if v:
                return json.loads(v)
        except Exception:
            return None
    return None


def _inputs_from_body(body: dict):
    """Accept {"url": ...} / {"alias": ...} / {"urls": [...]} / {"aliases": [...]}"""
    items = []
    if body.get("urls"):
        items = list(body["urls"])
    elif body.get("aliases"):
        items = list(body["aliases"])
    elif body.get("url"):
        items = [body["url"]]
    elif body.get("alias"):
        items = [body["alias"]]
    return [str(i) for i in items if i]


@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Yelp API via Bright Data prebuilt scrapers",
        "model": "async — trigger a job, then poll /result/{job_id}",
        "endpoints": {
            "POST /business/collect": "trigger business detail collection by URL/alias",
            "POST /reviews/collect": "trigger reviews collection by URL/alias",
            "GET /result/{job_id}": "poll job status / fetch results",
            "GET /docs": "interactive API docs",
        },
    }


@app.post("/business/collect")
@limiter.limit("50/second")
async def collect_business(
    request: Request,
    body: dict = Body(
        ...,
        example={"url": "https://www.yelp.com/biz/blue-bottle-coffee-san-francisco-8"},
    ),
):
    """Trigger a Yelp **business detail** collection. Returns a job_id to poll."""
    try:
        urls = _inputs_from_body(body)
        if not urls:
            return {"status": "error", "message": "Provide 'url'/'alias' or 'urls'/'aliases'."}
        y = BrightDataYelp()
        snapshot_id = y.trigger_business(urls)
        _save_job(snapshot_id, {"type": "business", "inputs": [alias_to_url(u) for u in urls], "created": time.time()})
        return {
            "status": "triggered",
            "type": "business",
            "job_id": snapshot_id,
            "poll": f"/result/{snapshot_id}",
            "note": "Poll /result/{job_id} every ~10s; jobs take ~30s to a few minutes.",
        }
    except BrightDataError as be:
        return {"status": "error", "message": str(be)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.post("/reviews/collect")
@limiter.limit("50/second")
async def collect_reviews(
    request: Request,
    body: dict = Body(
        ...,
        example={"url": "https://www.yelp.com/biz/blue-bottle-coffee-san-francisco-8", "limit_per_input": 20},
    ),
):
    """Trigger a Yelp **reviews** collection. Returns a job_id to poll."""
    try:
        urls = _inputs_from_body(body)
        if not urls:
            return {"status": "error", "message": "Provide 'url'/'alias' or 'urls'/'aliases'."}
        limit = body.get("limit_per_input")
        y = BrightDataYelp()
        if not y.reviews_dataset:
            return {
                "status": "error",
                "message": "BRIGHTDATA_YELP_REVIEWS_DATASET is not set. Add the Yelp Reviews scraper's gd_... id as an env var.",
            }
        snapshot_id = y.trigger_reviews(urls, limit_per_input=limit)
        _save_job(snapshot_id, {"type": "reviews", "inputs": [alias_to_url(u) for u in urls], "created": time.time()})
        return {
            "status": "triggered",
            "type": "reviews",
            "job_id": snapshot_id,
            "poll": f"/result/{snapshot_id}",
            "note": "Poll /result/{job_id} every ~10s; jobs take ~30s to a few minutes.",
        }
    except BrightDataError as be:
        return {"status": "error", "message": str(be)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/result/{job_id}")
@limiter.limit("50/second")
async def get_result(
    request: Request,
    job_id: str = Path(..., description="job_id (snapshot_id) returned by a /collect call"),
):
    """Poll a job. Returns status=running until data is ready, then the records."""
    try:
        cached = _cached_result(job_id)
        if cached:
            return {**cached, "cached": True}

        meta = _load_job(job_id)
        y = BrightDataYelp()
        res = y.result(job_id)
        res["job_id"] = job_id
        if meta.get("type"):
            res["type"] = meta["type"]

        if res.get("status") == "ready":
            payload = {
                "status": "ready",
                "job_id": job_id,
                "type": meta.get("type"),
                "results_count": len(res.get("records") or []),
                "records": res.get("records"),
            }
            _cache_result(job_id, payload)
            return {**payload, "cached": False}
        return res
    except BrightDataError as be:
        return {"status": "error", "message": str(be)}
    except Exception as e:
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
