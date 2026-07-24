"""
Crawlbase Crawling API fetch layer.

Crawlbase is a synchronous unblocking proxy: you GET/POST
    https://api.crawlbase.com/?token=TOKEN&url=<encoded target>
and it returns the target page (handling proxies, anti-bot, optional JS render).

We request `format=json`, so Crawlbase wraps the response as:
    {"original_status": 200, "pc_status": 200, "url": "...", "body": "<html>"}
- pc_status       = Crawlbase's own status (200 => Crawlbase succeeded)
- original_status = the target site's status (what Yelp returned)
- body            = the actual page content we parse

Config (env):
    CRAWLBASE_TOKEN       required — normal token (or JS token if you only have that)
    CRAWLBASE_JS_TOKEN    optional — JavaScript token, used when JS rendering is on
    CRAWLBASE_JAVASCRIPT  optional — "true" to render with a real browser (needs JS token)
    CRAWLBASE_COUNTRY     optional — geolocate exit IP (default US)
    REQUEST_TIMEOUT       optional — HTTP timeout seconds (default 90)
"""

import json
import os
from urllib.parse import urlencode

from primp import Client as PrimpClient

CRAWLBASE_BASE = "https://api.crawlbase.com/"


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "on")


class CrawlbaseError(Exception):
    pass


class Crawlbase:
    def __init__(self, timeout: int = 90):
        self.token = os.getenv("CRAWLBASE_TOKEN")
        self.js_token = os.getenv("CRAWLBASE_JS_TOKEN")
        self.use_js = _truthy(os.getenv("CRAWLBASE_JAVASCRIPT"))
        self.country = os.getenv("CRAWLBASE_COUNTRY", "US")
        self.timeout = int(os.getenv("REQUEST_TIMEOUT", timeout))
        if not (self.token or self.js_token):
            raise CrawlbaseError("CRAWLBASE_TOKEN is not set")

    def _token(self) -> str:
        if self.use_js and self.js_token:
            return self.js_token
        return self.token or self.js_token

    def _params(self, target: str, extra: dict = None) -> dict:
        p = {
            "token": self._token(),
            "url": target,
            "format": "json",
            "country": self.country,
        }
        if self.use_js:
            p["javascript"] = "true"
        if extra:
            p.update({k: str(v) for k, v in extra.items()})
        return p

    def get(self, target: str, extra: dict = None) -> str:
        c = PrimpClient(timeout=self.timeout)
        r = c.get(f"{CRAWLBASE_BASE}?{urlencode(self._params(target, extra))}")
        return self._handle(r, target)

    def post(self, target: str, body, content_type: str = "json", extra: dict = None) -> str:
        c = PrimpClient(timeout=self.timeout)
        params = self._params(target, extra)
        params["post_content_type"] = content_type
        if isinstance(body, str):
            body = body.encode("utf-8")
        r = c.post(
            f"{CRAWLBASE_BASE}?{urlencode(params)}",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        return self._handle(r, target)

    def _handle(self, r, target: str) -> str:
        if r.status_code != 200:
            raise CrawlbaseError(
                f"Crawlbase HTTP {r.status_code}: {r.text[:200]} "
                f"(check token / credits)"
            )
        # format=json wraps the target response; fall back to raw text if not JSON.
        try:
            data = json.loads(r.text)
        except json.JSONDecodeError:
            return r.text

        pc = data.get("pc_status")
        orig = data.get("original_status")
        body = data.get("body", "")

        if pc is not None and int(pc) != 200:
            raise CrawlbaseError(f"Crawlbase pc_status={pc} for {target} (request failed upstream)")
        if orig is not None and int(orig) >= 400:
            raise CrawlbaseError(f"Yelp returned {orig} for {target} (blocked or not found)")
        return body
