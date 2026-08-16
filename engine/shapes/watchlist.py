"""Watchlist board: a fixed set of markets, same layout every day.

Config (in audience.yml under the shape):
  watch:
    - name: "Senate control (D)"
      match: {venue: kalshi, ticker: KXSENATE...}      # exact
      # or: {venue: polymarket, title_regex: "Democrats.*Senate"}
  headline: "Midterms 2026 odds board"
  countdown_to: "2026-11-03"        # optional; adds "N days to Election Day"
  countdown_label: "Election Day"
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from ..model import Post
from .base import Context, Shape, cents, delta_pts, fit_text, pts

log = logging.getLogger("engine.shapes.watchlist")


class WatchlistShape(Shape):
    name = "watchlist"

    def _find(self, rows, m: dict):
        venue = m.get("venue")
        for r in rows:
            if venue and r.venue != venue:
                continue
            if m.get("ticker") and r.ticker == m["ticker"]:
                return r
            if m.get("title_regex") and re.search(m["title_regex"], r.title + " " + r.subtitle, re.I):
                if not m.get("subtitle_regex") or re.search(m["subtitle_regex"], r.subtitle, re.I):
                    return r
        return None

    def build(self, ctx: Context) -> Optional[Post]:
        watch = self.cfg.get("watch") or []
        found = []
        for w in watch:
            hits = []
            for m in w.get("match") or []:
                r = self._find(ctx.open_rows, m)
                if r is None:
                    log.warning("watchlist %r: no row for match %s", w.get("name"), m)
                    continue
                hits.append(r)
            if hits:
                found.append((w, hits))
        if len(found) < int(self.cfg.get("min_found", 2)):
            return None
        headline = self.cfg.get("headline", "Odds board")
        if self.cfg.get("countdown_to"):
            target = datetime.fromisoformat(self.cfg["countdown_to"]).replace(tzinfo=timezone.utc)
            days = (target - ctx.now).days
            if days >= 0:
                headline += f" · {days} days to {self.cfg.get('countdown_label', 'the date')}"
        lines = []
        table = []
        for w, hits in found:
            parts = []
            for r in hits:
                label = ctx.labels.get(r.venue, r.venue[:1].upper())
                if r.prev_24h_price is not None:
                    move = delta_pts(r.prev_24h_price, r.yes_price)
                elif r.change_24h is not None:
                    move = pts(r.change_24h)
                else:
                    move = None
                parts.append(f"{label} {cents(r.yes_price)}" + (f" ({move})" if move else ""))
                table.append({"name": w["name"], "venue": r.venue, "label": label, "ticker": r.ticker,
                              "title": r.title, "subtitle": r.subtitle, "now": r.yes_price,
                              "prev": r.prev_24h_price, "delta": r.change_24h, "url": r.url,
                              "volume_24h": r.volume_24h})
            lines.append(f"{w['name']}: " + " · ".join(parts))
        tags = list(self.cfg.get("tags") or ctx.audience.tags[:2])
        text = fit_text(headline, lines, " ".join(f"#{t}" for t in tags), what=f"watchlist {ctx.slot}")
        pid = f"{ctx.now.strftime('%Y-%m-%d')}-{ctx.slot}"
        return Post(
            id=pid, slot=ctx.slot, shape=self.name,
            title=f"{self.cfg.get('headline', 'Odds board')} — {ctx.now.strftime('%b %-d, %Y')}",
            text=text, published_at=ctx.now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            tags=tags, rows=table, extra={"chart_kind": "board", "headline": headline},
        )
