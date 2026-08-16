from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("engine.channels")


class Channel:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def publish(self, audience, post: dict, env: dict, prev: dict | None = None) -> dict:
        """Return {"ok": bool, ...}. Never raise for expected failures.

        `prev` is the record this channel wrote for this post on an earlier run,
        or None. A channel uses it to carry a counter or to verify a delivery
        instead of sending the same thing twice.
        """
        raise NotImplementedError


def probe_live(url: str, must_contain: str, timeout_s: int = 20) -> bool:
    """One GET. True when the page returns 200 and holds `must_contain`."""
    try:
        r = requests.get(url, timeout=timeout_s, headers={"User-Agent": "attention-engine/0.1 (live-check)"})
    except requests.RequestException as e:
        log.info("live check error: %s", e)
        return False
    if r.status_code == 200 and must_contain in r.text:
        return True
    log.info("not live yet: %s -> %s", url, r.status_code)
    return False


def wait_until_live(url: str, must_contain: str, timeout_s: int = 300, every_s: int = 15) -> bool:
    """Poll `url` until it returns 200 and contains `must_contain`."""
    deadline = time.time() + timeout_s
    while True:
        if probe_live(url, must_contain):
            return True
        if time.time() + every_s >= deadline:
            return False
        time.sleep(every_s)
