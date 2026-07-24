# Yelp API (Unofficial, via Crawlbase)

FastAPI wrapper that fetches Yelp's own pages/endpoints through the **Crawlbase Crawling API** (which handles proxies + anti-bot), then parses them into clean JSON. Synchronous: one request in, data out. Includes Redis caching and per-IP rate limiting.

Yelp blocks direct scraping hard (plain datacenter/ISP proxies → 403). Crawlbase acts as the unblocking layer so the same requests succeed.

## Endpoints

| Method | Path | Yelp source (fetched via Crawlbase) |
|---|---|---|
| GET | `/search` | `/search?find_desc=&find_loc=` → embedded `react_root_props` |
| GET | `/business/{id}` | `/biz/{alias}` → embedded JSON + `yelp-biz-id` |
| GET | `/reviews` | `POST /gql/batch` GraphQL `GetBusinessReviewFeed` |
| GET | `/autocomplete` | `/search_suggest/v2/prefetch` |

## Run

```bash
pip install -r requirements.txt
export CRAWLBASE_TOKEN=your_token
uvicorn main:app --reload
# docs at http://localhost:8000/docs
```

Docker:

```bash
docker build -t yelp-api .
docker run -p 8080:8080 --env-file .env yelp-api
```

## Usage

### GET /search
```
/search?term=coffee&location=San Francisco, CA
/search?term=plumbers&location=Seattle, WA&offset=10&sort_by=rating&price=1,2
```
Returns per business: `id` (encoded biz id — pass to `/reviews`), `alias`, `name`, `url`, `rating`, `review_count`, `price`, `categories`, `phone`, `address`, `latitude`, `longitude`, `photo`.

### GET /business/{id}
```
/business/blue-bottle-coffee-san-francisco-8
```
Accepts a business **alias** or **encoded biz id**. Returns name, rating, review count, price, categories, phone, website, address, coordinates, `hours`, `is_claimed`, photos, attributes.

### GET /reviews
```
/reviews?business_id=Lw7NmZ3j-WEye97ywEmkXQ
/reviews?business_id=blue-bottle-coffee-san-francisco-8&offset=10&sort_by=RATING_DESC
```
`business_id` = the encoded id from `/search` (an alias also works — it's resolved first). Params: `offset`, `limit` (≤50), `sort_by`, `language`.

### GET /autocomplete
```
/autocomplete?prefix=coff&location=San Francisco, CA
```
Returns `terms`, `businesses`, `categories`.

## Config (.env)

| Var | Purpose |
|---|---|
| `CRAWLBASE_TOKEN` | **Required.** Crawlbase Normal token |
| `CRAWLBASE_JS_TOKEN` | Crawlbase JavaScript token (only if `CRAWLBASE_JAVASCRIPT=true`) |
| `CRAWLBASE_JAVASCRIPT` | `true` to render with a real browser (default false; Yelp is server-rendered) |
| `CRAWLBASE_COUNTRY` | Exit-IP country (default US) |
| `REQUEST_TIMEOUT` | Crawlbase HTTP timeout, seconds (default 90) |
| `YELP_REVIEW_DOC_ID` | Override reviews GraphQL persisted-query hash if Yelp rotates it |
| `REDIS_*`, `CACHE_TTL`, `REVIEW_TTL` | Optional response cache |

## Notes

- Runs fine without Redis — caching just disables itself.
- Errors surface as `{"status":"error","message":"..."}`. A Crawlbase `pc_status`≠200 means the fetch failed (token/credits); a Yelp `original_status`≥400 means Yelp blocked or the page wasn't found.
- If `/search` or `/business` returns empty, try `CRAWLBASE_JAVASCRIPT=true` (with a JS token) — some pages occasionally need rendering.
- If `/reviews` returns empty, refresh `YELP_REVIEW_DOC_ID` (Yelp rotates the GraphQL hash).
- Deployment note: keep responses under your reverse proxy's timeout. Crawlbase without JS rendering is usually a few seconds; enabling JS can push it higher.
