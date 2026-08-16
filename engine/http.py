"""HTTP session with a declared User-Agent, retries, and backoff."""
from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("engine.http")


class Http:
    def __init__(self, user_agent: str, timeout: int = 30):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = user_agent
        self.timeout = timeout

    def get_json(self, url: str, params: dict | None = None, tries: int = 5,
                 min_sleep: float = 0.0):
        """GET JSON with exponential backoff on 429/5xx/network errors."""
        delay = 1.0
        last = None
        for attempt in range(1, tries + 1):
            try:
                r = self.s.get(url, params=params, timeout=self.timeout)
                if r.status_code in (429, 500, 502, 503, 504, 403):
                    raise requests.HTTPError(f"{r.status_code} for {r.url}", response=r)
                r.raise_for_status()
                if min_sleep:
                    time.sleep(min_sleep)
                return r.json()
            except (requests.RequestException, ValueError) as e:
                last = e
                log.warning("GET %s failed (try %d/%d): %s", url, attempt, tries, e)
                if attempt < tries:
                    time.sleep(delay)
                    delay = min(delay * 2, 16)
        raise RuntimeError(f"GET {url} failed after {tries} tries: {last}")

    def get(self, url: str, **kw):
        return self.s.get(url, timeout=self.timeout, **kw)

    def post(self, url: str, **kw):
        return self.s.post(url, timeout=self.timeout, **kw)
