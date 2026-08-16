from __future__ import annotations

import logging
import time

import requests

log = logging.getLogger("engine.channels")


class Channel:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def publish(self, audience, post: dict, env: dict) -> dict:
        """Return {"ok": bool, ...}. Never raise for expected failures."""
        raise NotImplementedError


def wait_until_live(url: str, must_contain: str, timeout_s: int = 600, every_s: int = 15) -> bool:
    """Poll `url` until it returns 200 and contains `must_contain`."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "attention-engine/0.1 (live-check)"})
            if r.status_code == 200 and must_contain in r.text:
                return True
            log.info("not live yet: %s -> %s", url, r.status_code)
        except requests.RequestException as e:
            log.info("live check error: %s", e)
        time.sleep(every_s)
    return False
