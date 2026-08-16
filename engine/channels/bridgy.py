"""Bridgy Fed web-site bridge: one webmention per post -> Bluesky + fediverse.

Verified from fed.brid.gy docs (2026-08-16): a web site opts in with
POST https://fed.brid.gy/web-site (url=<domain>); each new post is pushed
with POST https://fed.brid.gy/webmention (source=<post URL>,
target=https://fed.brid.gy/). The post page must link https://fed.brid.gy/
(class u-bridgy-fed) and carry an h-entry.

The webmention endpoint answers 202 as soon as it queues the work. It never
reports the result of the queued work. So 202 means "accepted", not "posted".
The channel confirms a real delivery with GET web.brid.gy/convert/bsky/<source>,
which returns 200 only after the Bluesky copy exists.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import requests

from .base import Channel, probe_live, wait_until_live

log = logging.getLogger("engine.channels.bridgy")
BRIDGY = "https://fed.brid.gy"
CONVERT = "https://web.brid.gy/convert/bsky"
MARKER = "u-bridgy-fed"
# Attempts of the webmention itself before the channel gives up on a post.
MAX_ATTEMPTS = 3
# Checks of the convert endpoint inside one run, and the pause between them.
VERIFY_TRIES = 3
VERIFY_SLEEP_S = 20


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class BridgyChannel(Channel):
    name = "bridgy"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        # Per-run flags. build_channels makes one instance for each run.
        self._waited = False        # one full liveness wait per run, no more
        self._site_down = False     # the site failed that wait; skip the rest
        self._enabled_site = False  # POST /web-site already ran in this run

    # ------------------------------------------------------------------ live
    def _page_live(self, source: str) -> bool:
        """True when the post page is live. At most one long wait per run."""
        if self._site_down:
            log.warning("site is down this run; skipping %s", source)
            return False
        if self._waited:
            return probe_live(source, MARKER)
        self._waited = True
        timeout_s = int(self.cfg.get("live_timeout_s", 300))
        live = wait_until_live(source, MARKER, timeout_s=timeout_s)
        if not live:
            self._site_down = True
        return live

    # ---------------------------------------------------------------- verify
    def _verify(self, source: str, tries: int = VERIFY_TRIES) -> tuple[bool, str | None]:
        """Ask Bridgy Fed whether the Bluesky copy exists. Return (ok, at_uri)."""
        for i in range(tries):
            if i:
                time.sleep(VERIFY_SLEEP_S)
            try:
                r = requests.get(f"{CONVERT}/{source}", timeout=30, allow_redirects=False,
                                 headers={"User-Agent": "attention-engine/0.1 (bridge-check)"})
            except requests.RequestException as e:
                log.info("convert check error: %s", e)
                continue
            if r.status_code == 200:
                return True, self._at_uri(r)
            log.info("convert check %s -> %s", source, r.status_code)
        return False, None

    @staticmethod
    def _at_uri(r: requests.Response) -> str | None:
        """Pull the at:// URI out of the Link header, when Bridgy sends one."""
        for part in (r.headers.get("Link") or "").split(","):
            url = part.split(";")[0].strip().strip("<>")
            if url.startswith("at://"):
                return url
        return None

    # ------------------------------------------------------------ enablement
    def _enable_site(self, audience) -> str | None:
        """POST /web-site once. Bridgy Fed needs the user before a webmention."""
        if self._enabled_site:
            return None
        self._enabled_site = True
        try:
            r = requests.post(f"{BRIDGY}/web-site", data={"url": audience.domain}, timeout=60,
                              headers={"User-Agent": f"{audience.display_name} publisher"})
        except requests.RequestException as e:
            log.warning("web-site enable failed: %s", e)
            return None
        log.info("web-site enable %s -> %s", audience.domain, r.status_code)
        if r.status_code in (200, 201, 202, 302):
            return _now()
        return None

    # --------------------------------------------------------------- publish
    def publish(self, audience, post: dict, env: dict, prev: dict | None = None) -> dict:
        source = f"{audience.site_url}/p/{post['id']}"
        prev = prev or {}
        attempts = int(prev.get("attempts") or 0)

        # An earlier run got a 202 but never saw the Bluesky copy. Check first:
        # a repeat webmention is pointless when the post already landed.
        if prev.get("accepted"):
            ok, at_uri = self._verify(source)
            if ok:
                return {"ok": True, "accepted": True, "attempts": attempts, "source": source,
                        "at_uri": at_uri, "verified_at": _now()}
            if attempts >= MAX_ATTEMPTS:
                log.warning("bridgy gave up on %s after %d attempts", source, attempts)
                return {"ok": False, "accepted": True, "gave_up": True, "attempts": attempts,
                        "source": source, "error": f"not verified after {attempts} attempts"}

        if not self._page_live(source):
            return {"ok": False, "error": "site not live; skipped", "source": source, "attempts": attempts,
                    "accepted": bool(prev.get("accepted"))}

        record = self._send(audience, source, attempts + 1)
        if record.get("enable_needed"):
            # Bridgy Fed has no user for this domain yet. Create it, then resend.
            record.pop("enable_needed")
            enabled_at = self._enable_site(audience)
            if enabled_at:
                record = self._send(audience, source, attempts + 1)
                record["bridgy_enabled_at"] = enabled_at
        if record.get("accepted"):
            ok, at_uri = self._verify(source)
            record["ok"] = ok
            if at_uri:
                record["at_uri"] = at_uri
            if ok:
                record["verified_at"] = _now()
        return record

    def _send(self, audience, source: str, attempt: int) -> dict:
        """POST one webmention. 2xx means accepted, not delivered."""
        try:
            r = requests.post(f"{BRIDGY}/webmention", data={"source": source, "target": f"{BRIDGY}/"},
                              timeout=60, headers={"User-Agent": f"{audience.display_name} publisher"})
        except requests.RequestException as e:
            return {"ok": False, "accepted": False, "attempts": attempt, "source": source,
                    "error": f"{type(e).__name__}: {e}"}
        body = r.text[:400]
        log.info("webmention %s -> %s %s", source, r.status_code, body.replace("\n", " ")[:200])
        accepted = 200 <= r.status_code < 300
        record = {"ok": False, "accepted": accepted, "status": r.status_code, "body": body,
                  "attempts": attempt, "source": source}
        if r.status_code == 400 and "No user found" in body:
            record["enable_needed"] = True
        return record
