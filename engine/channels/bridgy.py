"""Bridgy Fed web-site bridge: one webmention per post -> Bluesky + fediverse.

Verified from fed.brid.gy docs (2026-08-16): a web site opts in with
POST https://fed.brid.gy/web-site (url=<domain>); each new post is pushed
with POST https://fed.brid.gy/webmention (source=<post URL>,
target=https://fed.brid.gy/). The post page must link https://fed.brid.gy/
(class u-bridgy-fed) and carry an h-entry.
"""
from __future__ import annotations

import logging

import requests

from .base import Channel, wait_until_live

log = logging.getLogger("engine.channels.bridgy")
BRIDGY = "https://fed.brid.gy"


class BridgyChannel(Channel):
    name = "bridgy"

    def publish(self, audience, post: dict, env: dict) -> dict:
        source = f"{audience.site_url}/p/{post['id']}"
        if not wait_until_live(source, "u-bridgy-fed", timeout_s=int(self.cfg.get("live_timeout_s", 600))):
            return {"ok": False, "error": "post page not live in time", "source": source}
        r = requests.post(f"{BRIDGY}/webmention", data={"source": source, "target": f"{BRIDGY}/"},
                          timeout=60, headers={"User-Agent": f"{audience.display_name} publisher"})
        ok = r.status_code in (200, 201, 202)
        body = r.text[:400]
        log.info("webmention %s -> %s %s", source, r.status_code, body.replace("\n", " ")[:200])
        return {"ok": ok, "status": r.status_code, "body": body, "source": source}
