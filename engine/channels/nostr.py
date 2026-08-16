"""Nostr channel: shells out to engine/channels/nostr_publish.mjs (Node)."""
from __future__ import annotations

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .base import Channel

log = logging.getLogger("engine.channels.nostr")
SCRIPT = Path(__file__).with_name("nostr_publish.mjs")


def created_at_epoch(published_at: str | None) -> int | None:
    """Turn an ISO-8601 timestamp into a Unix second count.

    The note uses the post time, not the run time. Content, tags, and
    created_at are then the same on every attempt, so the event id repeats and
    a relay drops the duplicate instead of storing a second note.
    """
    if not published_at:
        return None
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        log.warning("cannot parse published_at %r; the note gets a fresh id", published_at)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


class NostrChannel(Channel):
    name = "nostr"

    def publish(self, audience, post: dict, env: dict, prev: dict | None = None) -> dict:
        nsec = env.get("NOSTR_NSEC") or os.environ.get("NOSTR_NSEC")
        if not nsec:
            return {"ok": False, "error": "NOSTR_NSEC not set"}
        url = f"{audience.site_url}/p/{post['id']}"
        cmd = ["node", str(SCRIPT), "note", "--content", post["text"], "--url", url]
        created_at = created_at_epoch(post.get("published_at"))
        if created_at is not None:
            cmd += ["--created-at", str(created_at)]
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
        # The script prints one pretty-printed JSON report. Read all of stdout.
        out: dict = {}
        text = p.stdout.strip()
        if text:
            try:
                out = json.loads(text)
            except ValueError:
                out = {"raw": p.stdout[-300:]}
        ok = p.returncode == 0
        if not ok:
            log.warning("nostr publish failed rc=%s stderr=%s", p.returncode, p.stderr[-300:])
        return {
            "ok": ok,
            "id": out.get("id"),
            "relays_ok": out.get("ok"),
            "failed": out.get("failed"),
            "created_at": created_at,
            "raw": out.get("raw"),
            "error": p.stderr[-300:] if not ok else None,
        }
