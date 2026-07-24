# Yelp API (via Bright Data prebuilt scrapers)

FastAPI wrapper around Bright Data's **prebuilt Yelp Web Scraper API** for reliable business & review data. Yelp aggressively blocks live scraping (plain proxies → 403, and even Web Unlocker times out on Yelp's search page), so this uses Bright Data's maintained Yelp datasets instead — they handle the unblocking and return structured JSON.

## Model: asynchronous jobs

Bright Data's scraper is a batch/async API — a collection takes ~30s to a few minutes. So this API is **job-based**:

1. `POST /business/collect` or `POST /reviews/collect` → returns a `job_id`
2. `GET /result/{job_id}` → poll every ~10s; returns `status: running` until data is ready, then the records

Scope: **business details** and **reviews**, collected by business **URL or alias**. (Keyword search and autocomplete aren't covered by the prebuilt scraper — those were the exact pages that defeated live scraping.)

## Run

```bash
pip install -r requirements.txt
export BRIGHTDATA_API_KEY=your_key
uvicorn main:app --reload
# docs at http://localhost:8000/docs
```

Docker:

```bash
docker build -t yelp-api .
docker run -p 8080:8080 --env-file .env yelp-api
```

## Endpoints

### POST /business/collect

Trigger a business-detail collection.

```json
{ "url": "https://www.yelp.com/biz/blue-bottle-coffee-san-francisco-8" }
```

Also accepts `{"alias": "blue-bottle-coffee-san-francisco-8"}`, or batches via `{"urls": [...]}` / `{"aliases": [...]}`. Returns:

```json
{ "status": "triggered", "type": "business", "job_id": "s_xxx", "poll": "/result/s_xxx" }
```

### POST /reviews/collect

Trigger a reviews collection. Requires `BRIGHTDATA_YELP_REVIEWS_DATASET` to be set.

```json
{ "url": "https://www.yelp.com/biz/blue-bottle-coffee-san-francisco-8", "limit_per_input": 20 }
```

### GET /result/{job_id}

Poll a job.

```json
// still working:
{ "status": "running", "raw_status": "collecting", "job_id": "s_xxx" }

// done:
{ "status": "ready", "job_id": "s_xxx", "type": "business", "results_count": 1, "records": [ ... ] }
```

`records` is Bright Data's structured Yelp output (business name, rating, review count, address, hours, photos, etc. — or review objects for the reviews scraper). Finished results are cached in Redis (`RESULT_TTL`, default 24h), so re-polling a completed job is instant.

## Config (.env)

| Var | Purpose |
|---|---|
| `BRIGHTDATA_API_KEY` | **Required.** Bright Data API key |
| `BRIGHTDATA_YELP_BUSINESS_DATASET` | Business scraper dataset id (default `gd_lgugwl0519h1p14rwk`) |
| `BRIGHTDATA_YELP_REVIEWS_DATASET` | Reviews scraper dataset id (set this yourself) |
| `REQUEST_TIMEOUT` | HTTP timeout for Bright Data calls (default 30s) |
| `REDIS_*`, `RESULT_TTL`, `JOB_TTL` | Optional job registry + result cache |

Find a dataset id by opening the scraper in the Bright Data control panel — it's the `gd_...` in the URL.

## Notes

- Runs fine without Redis; it just won't label a job's `type` or cache finished results.
- The `/collect` calls are fast (they only *trigger* a job). Only Bright Data's collection is slow, and that happens out of band — so this API never holds a long HTTP request open, which avoids the reverse-proxy (502/504) timeouts that block synchronous Yelp scraping.
- Billing is per record collected (see your Bright Data plan; the Yelp scrapers include a monthly free tier).
