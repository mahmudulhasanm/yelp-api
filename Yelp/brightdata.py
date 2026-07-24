"""
Bright Data Web Scraper API client for Yelp (prebuilt datasets).

Unlike a proxy/unlocker, this is an ASYNC batch model:

    1. trigger(dataset_id, inputs)  -> snapshot_id     (POST /datasets/v3/trigger)
    2. progress(snapshot_id)        -> status          (GET  /datasets/v3/progress/{id})
    3. snapshot(snapshot_id)        -> records          (GET  /datasets/v3/snapshot/{id})

A job typically takes ~30s to a few minutes, so the API layer exposes this as
jobs the caller polls, rather than blocking one HTTP request on it.

Dataset IDs come from each scraper's page in the Bright Data control panel
(the `gd_...` in the URL). Configure them via env:
    BRIGHTDATA_API_KEY               (required)
    BRIGHTDATA_YELP_BUSINESS_DATASET (default: gd_lgugwl0519h1p14rwk)
    BRIGHTDATA_YELP_REVIEWS_DATASET  (set to your Yelp Reviews scraper id)
"""

import json
import os
from typing import List, Optional

from primp import Client as PrimpClient

API_BASE = "https://api.brightdata.com/datasets/v3"

# Yelp "Businesses" prebuilt scraper (collect by business URL).
DEFAULT_BUSINESS_DATASET = "gd_lgugwl0519h1p14rwk"

YELP_BIZ_BASE = "https://www.yelp.com/biz/"


class BrightDataError(Exception):
    pass


def alias_to_url(id_or_url: str) -> str:
    """Accept a full Yelp biz URL, a `/biz/...` path, or a bare alias."""
    s = (id_or_url or "").strip()
    if s.startswith("http"):
        return s
    if "/biz/" in s:
        return YELP_BIZ_BASE + s.split("/biz/")[-1].lstrip("/")
    return YELP_BIZ_BASE + s.lstrip("/")


class BrightDataYelp:
    def __init__(self, api_key: Optional[str] = None, timeout: int = 30):
        self.api_key = api_key or os.getenv("BRIGHTDATA_API_KEY")
        if not self.api_key:
            raise BrightDataError("BRIGHTDATA_API_KEY is not set")
        self.business_dataset = os.getenv("BRIGHTDATA_YELP_BUSINESS_DATASET", DEFAULT_BUSINESS_DATASET)
        self.reviews_dataset = os.getenv("BRIGHTDATA_YELP_REVIEWS_DATASET")
        self.timeout = int(os.getenv("REQUEST_TIMEOUT", timeout))

    # -- low-level ---------------------------------------------------------
    def _client(self) -> PrimpClient:
        return PrimpClient(timeout=self.timeout)

    def _headers(self, json_body: bool = False) -> dict:
        h = {"Authorization": f"Bearer {self.api_key}"}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def _trigger(self, dataset_id: str, inputs: List[dict], extra_params: dict = None) -> str:
        if not dataset_id:
            raise BrightDataError(
                "Dataset ID missing. Set BRIGHTDATA_YELP_BUSINESS_DATASET / "
                "BRIGHTDATA_YELP_REVIEWS_DATASET (the gd_... id from the scraper page)."
            )
        from urllib.parse import urlencode
        params = {"dataset_id": dataset_id, "include_errors": "true"}
        if extra_params:
            params.update(extra_params)
        url = f"{API_BASE}/trigger?{urlencode(params)}"
        r = self._client().post(
            url,
            content=json.dumps(inputs).encode("utf-8"),
            headers=self._headers(json_body=True),
        )
        if r.status_code != 200:
            raise BrightDataError(f"trigger -> HTTP {r.status_code}: {r.text[:300]}")
        data = json.loads(r.text)
        sid = data.get("snapshot_id")
        if not sid:
            raise BrightDataError(f"trigger returned no snapshot_id: {r.text[:300]}")
        return sid

    # -- public: trigger jobs ---------------------------------------------
    def trigger_business(self, urls: List[str]) -> str:
        inputs = [{"url": alias_to_url(u)} for u in urls]
        return self._trigger(self.business_dataset, inputs)

    def trigger_reviews(self, urls: List[str], limit_per_input: Optional[int] = None) -> str:
        inputs = [{"url": alias_to_url(u)} for u in urls]
        extra = {"limit_per_input": str(limit_per_input)} if limit_per_input else None
        return self._trigger(self.reviews_dataset, inputs, extra_params=extra)

    # -- public: poll + fetch ---------------------------------------------
    def progress(self, snapshot_id: str) -> dict:
        r = self._client().get(
            f"{API_BASE}/progress/{snapshot_id}",
            headers=self._headers(),
        )
        if r.status_code != 200:
            raise BrightDataError(f"progress -> HTTP {r.status_code}: {r.text[:300]}")
        return json.loads(r.text)

    def snapshot(self, snapshot_id: str, fmt: str = "json"):
        r = self._client().get(
            f"{API_BASE}/snapshot/{snapshot_id}?format={fmt}",
            headers=self._headers(),
        )
        # 202 = snapshot not ready yet.
        if r.status_code == 202:
            return None
        if r.status_code != 200:
            raise BrightDataError(f"snapshot -> HTTP {r.status_code}: {r.text[:300]}")
        try:
            return json.loads(r.text)
        except json.JSONDecodeError:
            # ndjson fallback
            return [json.loads(line) for line in r.text.splitlines() if line.strip()]

    def result(self, snapshot_id: str) -> dict:
        """Combined status+data helper for the /result endpoint.

        Returns one of:
          {"status": "running"}          -> keep polling
          {"status": "ready", "records": [...]}
          {"status": "failed", "detail": ...}
        """
        prog = self.progress(snapshot_id)
        status = (prog.get("status") or "").lower()
        if status in ("running", "collecting", "building", "pending", "queued", ""):
            return {"status": "running", "raw_status": status or "pending"}
        if status in ("failed", "error", "canceled", "cancelled"):
            return {"status": "failed", "detail": prog}
        # ready / done -> download
        records = self.snapshot(snapshot_id)
        if records is None:
            return {"status": "running", "raw_status": "finalizing"}
        return {"status": "ready", "records": records}
