# Yelp API (Unofficial)

FastAPI wrapper around Yelp's internal endpoints — same method as the Google Flights / Ads Transparency scrapers: direct requests to Yelp's own data endpoints with a Chrome-impersonated TLS session (`primp`), no headless browser. Includes Redis caching and per-IP rate limiting.

**How it works**

- **Search** — `GET https://www.yelp.com/search/snippet` returns the search page with a `react_root_props` JSON blob; results come from `legacyProps.searchAppProps.searchPageProps.mainContentComponentsListProps`.
- **Business detail** — `GET /biz/{alias}`; encoded biz id from `<meta name="yelp-biz-id">`, detail from the same JSON blob (with regex fallbacks for the visible page).
- **Reviews** — `POST /gql/batch`, GraphQL `GetBusinessReviewFeed`, paginated via a base64 `after` offset cursor.
- **Autocomplete** — `GET /search_suggest/v2/prefetch` returns JSON suggestion groups.

## Run

```bash
pip install -r requirements.txt
uvicorn main:app --reload
# docs at http://localhost:8000/docs
```

Docker:

```bash
docker build -t yelp-api .
docker run -p 8080:8080 --env-file .env yelp-api
```

## Endpoints

### GET /search

```
/search?term=coffee&location=San+Francisco,+CA
/search?term=plumbers&location=Seattle,+WA&offset=10&sort_by=rating&price=1,2
```

Params: `term`, `location`, `offset` (page size 10), `limit` (≤10), `sort_by` (recommended | rating | review_count | distance), `price` (`1`..`4` or CSV).

Returns per business: `id` (encoded biz id — feed this to `/reviews`), `alias`, `name`, `url`, `rating`, `review_count`, `price`, `categories`, `phone`, `address`, `latitude`, `longitude`, `is_ad`, `photo`.

### GET /business/{id}

```
/business/vons-1000-spirits-seattle-4
```

Accepts a business **alias** (URL slug) or an **encoded biz id**. Returns name, rating, review count, price, categories, phone, website, address, coordinates, `hours` (per-day map), `is_claimed`, photos, and attributes.

### GET /reviews

```
/reviews?business_id=Lw7NmZ3j-WEye97ywEmkXQ
/reviews?business_id=vons-1000-spirits-seattle-4&offset=10&sort_by=RATING_DESC
```

`business_id` is the **encoded biz id** from `/search` results. An alias also works (it's resolved to the encoded id first, one extra request). Params: `offset`, `limit` (≤50), `sort_by` (DATE_DESC | DATE_ASC | RATING_DESC | RATING_ASC | ELITES_DESC), `language`.

Returns per review: `id`, `rating`, `text`, `date`, `author_name`, `author_location`, `author_review_count`, `feedback`, `photos`.

### GET /autocomplete

```
/autocomplete?prefix=coff&location=San+Francisco,+CA
```

Returns `terms`, `businesses`, and `categories` suggestion arrays.

## Config (.env)

`REDIS_HOST`, `REDIS_PORT`, `REDIS_PASSWORD`, `CACHE_TTL` (default 3600s), `REVIEW_TTL` (default 21600s), `PROXY`, `YELP_REVIEW_DOC_ID`.

Runs fine without Redis — caching just disables itself.

## Caveats

- Unofficial scraper. Yelp blocks aggressively and **datacenter IPs are frequently blocked** — use a residential/rotating `PROXY` for anything beyond light use. A non-200 from Yelp surfaces as `{"status": "error", "message": "... likely blocked; try a proxy"}`.
- The reviews GraphQL `documentId` is a persisted-query hash Yelp rotates. If `/reviews` stops returning data, capture a fresh `GetBusinessReviewFeed` request from browser DevTools (Network tab) and set `YELP_REVIEW_DOC_ID`.
- Page JSON structure drifts when Yelp updates its frontend. Parsing is defensive (`.get()` chains + regex fallbacks), but if a field goes null, re-capture the payload and adjust `Yelp/parser.py`.
- Respect Yelp's ToS and robots. For a stable, supported alternative, Yelp's official [Fusion API](https://docs.developer.yelp.com/) covers search, business, reviews and autocomplete with an API key.
