"""Mastodon channel (native account). Needs env MASTODON_INSTANCE + MASTODON_TOKEN.

Signup helper for a new bot account: tools/mastodon.mjs (email via AgentMail).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import requests

from .base import Channel

log = logging.getLogger("engine.channels.mastodon")


class MastodonChannel(Channel):
    name = "mastodon"

    def publish(self, audience, post: dict, env: dict, prev: dict | None = None) -> dict:
        inst = env.get("MASTODON_INSTANCE") or os.environ.get("MASTODON_INSTANCE") or self.cfg.get("instance")
        tok = env.get("MASTODON_TOKEN") or os.environ.get("MASTODON_TOKEN")
        if not (inst and tok):
            return {"ok": False, "error": "MASTODON_INSTANCE/MASTODON_TOKEN not set"}
        h = {"Authorization": f"Bearer {tok}"}
        media_ids = []
        if post.get("chart"):
            path = audience.site_dir / post["chart"].lstrip("/")
            if Path(path).exists():
                with open(path, "rb") as f:
                    r = requests.post(f"https://{inst}/api/v2/media", headers=h, files={"file": ("chart.png", f, "image/png")},
                                      data={"description": (post.get("chart_alt") or post["text"])[:1500]}, timeout=120)
                if r.status_code in (200, 202):
                    media_ids.append(r.json()["id"])
                else:
                    log.warning("media upload failed %s %s", r.status_code, r.text[:200])
        status = post["text"] + f"\n\n{audience.site_url}/p/{post['id']}"
        # Rack reads "media_ids[0]=x" as a Hash and Mastodon then drops it.
        # Only the repeated "media_ids[]" form arrives as an Array.
        data = [("status", status[:500]), ("visibility", "public"), ("language", "en")]
        data += [("media_ids[]", m) for m in media_ids]
        if post.get("chart") and not media_ids:
            log.warning("chart exists but no media id; the toot goes out as text only")
        r = requests.post(f"https://{inst}/api/v1/statuses", headers={**h, "Idempotency-Key": f"{audience.name}-{post['id']}"},
                          data=data, timeout=60)
        ok = r.status_code == 200
        return {"ok": ok, "status": r.status_code, "url": r.json().get("url") if ok else None, "body": None if ok else r.text[:300]}
