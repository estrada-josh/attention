"""Publish step: push every unpublished post through every channel once."""
from __future__ import annotations

import logging
import os

from .channels import build_channels
from .config import Audience
from .state import Store

log = logging.getLogger("engine.publish")


def publish(audience: Audience, only_post: str | None = None, dry_run: bool = False,
            max_age_hours: float = 36.0) -> dict:
    """Publish posts newer than max_age_hours that lack a record for a channel."""
    from datetime import datetime, timedelta, timezone
    store = Store(audience.data_dir)
    state = store.load_state()
    published = state.setdefault("published", {})
    channels = build_channels(audience.channels)
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    results = {}
    for post in sorted(store.load_posts(), key=lambda p: p["published_at"]):
        if only_post and post["id"] != only_post:
            continue
        if not only_post and post["published_at"] < cutoff:
            continue
        rec = published.setdefault(post["id"], {})
        for ch in channels:
            prev = rec.get(ch.name) or {}
            if prev.get("ok"):
                continue
            if prev.get("gave_up"):
                # The channel stopped trying for this post. Do not loop forever.
                continue
            if dry_run:
                log.info("DRY: would publish %s via %s", post["id"], ch.name)
                continue
            log.info("publishing %s via %s", post["id"], ch.name)
            try:
                res = ch.publish(audience, post, dict(os.environ), prev=prev)
            except Exception as e:  # noqa: BLE001
                res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            res["at"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
            # A channel may report a one-time account action. Keep it in state.
            if res.get("bridgy_enabled_at"):
                state["bridgy_enabled_at"] = res["bridgy_enabled_at"]
            rec[ch.name] = res
            results.setdefault(post["id"], {})[ch.name] = res
            store.save_state(state)
    return results
