"""Nostr channel: shells out to engine/channels/nostr_publish.mjs (Node)."""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from .base import Channel

log = logging.getLogger("engine.channels.nostr")
SCRIPT = Path(__file__).with_name("nostr_publish.mjs")


class NostrChannel(Channel):
    name = "nostr"

    def publish(self, audience, post: dict, env: dict) -> dict:
        nsec = env.get("NOSTR_NSEC") or os.environ.get("NOSTR_NSEC")
        if not nsec:
            return {"ok": False, "error": "NOSTR_NSEC not set"}
        url = f"{audience.site_url}/p/{post['id']}"
        cmd = ["node", str(SCRIPT), "note", "--content", post["text"], "--url", url]
        if post.get("chart"):
            cmd += ["--image", audience.site_url + post["chart"]]
        for t in post.get("tags") or []:
            cmd += ["--tag", t]
        e = dict(os.environ, NOSTR_NSEC=nsec)
        if self.cfg.get("relays"):
            e["NOSTR_RELAYS"] = ",".join(self.cfg["relays"])
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=e)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "nostr publish timed out"}
        try:
            out = json.loads(p.stdout.strip().splitlines()[-1]) if p.stdout.strip() else {}
        except (ValueError, IndexError):
            out = {"raw": p.stdout[-300:]}
        ok = p.returncode == 0
        if not ok:
            log.warning("nostr publish failed rc=%s stderr=%s", p.returncode, p.stderr[-300:])
        return {"ok": ok, **out}
