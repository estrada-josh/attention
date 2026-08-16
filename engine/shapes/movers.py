"""Movers: the biggest 24h odds swings across venues."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Optional

from ..model import Post
from .base import Context, Shape, cents, clip, fit_text, money, pts, score_move


class MoversShape(Shape):
    name = "movers"

    def candidates(self, ctx: Context) -> list:
        min_pts = float(ctx.th("movers_min_points", 8)) / 100.0
        min_vol = {v: float(x) for v, x in (ctx.th("min_volume_24h", {}) or {}).items()}
        default_vol = float(ctx.th("min_volume_24h_default", 5000))
        excl = set(ctx.th("exclude_categories", []) or [])
        title_rx = re.compile(ctx.th("exclude_title_regex", "$^"), re.I)
        min_hours = float(ctx.th("min_hours_to_close", 12))
        horizon = ctx.now + timedelta(hours=min_hours)
        out = []
        for r in ctx.open_rows:
            if r.change_24h is None or r.yes_price is None or r.prev_24h_price is None:
                continue
            if r.category in excl or title_rx.search(r.title):
                continue
            if (r.volume_24h or 0) < min_vol.get(r.venue, default_vol):
                continue
            if abs(r.change_24h) < min_pts:
                continue
            if r.close_time and r.close_time < horizon.strftime("%Y-%m-%dT%H:%M:%SZ"):
                continue
            # ignore markets pinned at 0/1 (already decided)
            if r.yes_price <= 0.005 or r.yes_price >= 0.995:
                continue
            out.append(r)
        out.sort(key=lambda r: score_move(r.change_24h, r.volume_24h or 0), reverse=True)
        # one market per event, keeps the list diverse
        seen_events = set()
        dedup = []
        for r in out:
            key = (r.venue, r.event_ticker or r.ticker)
            if key in seen_events:
                continue
            seen_events.add(key)
            dedup.append(r)
        return dedup

    def build(self, ctx: Context) -> Optional[Post]:
        rows = self.candidates(ctx)
        top_n = int(ctx.th("movers_top_n", 4))
        min_rows = int(ctx.th("movers_min_rows", 2))
        if len(rows) < min_rows:
            return None
        top = rows[:top_n]
        # 30-day record check
        hist = ctx.state.setdefault("movers_history", [])
        biggest = max(abs(r.change_24h) for r in top)
        cutoff = (ctx.now - timedelta(days=30)).strftime("%Y-%m-%d")
        recent = [h for h in hist if h["date"] >= cutoff]
        record = bool(recent) and biggest > max(h["max_abs"] for h in recent)
        hist[:] = recent + [{"date": ctx.now.strftime("%Y-%m-%d"), "max_abs": biggest}]

        header = "Biggest odds swings, last 24h"
        lines = []
        for r in top:
            label = ctx.labels.get(r.venue, r.venue[:1].upper())
            name = clip(r.title + (f" — {r.subtitle}" if r.subtitle else ""), 48)
            lines.append(f"{label} · {name} {cents(r.prev_24h_price)}→{cents(r.yes_price)} ({pts(r.change_24h)}) · {money(r.volume_24h)}")
        foot_lines = []
        if record:
            foot_lines.append(f"Largest 24h swing in 30 days: {pts(max(top, key=lambda r: abs(r.change_24h)).change_24h)}")
        tags = list(ctx.audience.tags[:1])
        venues = {r.venue for r in top}
        for v in sorted(venues):
            t = (ctx.th("venue_tags", {}) or {}).get(v)
            if t and len(tags) < 3:
                tags.append(t)
        foot_lines.append(" ".join(f"#{t}" for t in tags))
        text = fit_text(header, lines, "\n".join(foot_lines))
        pid = f"{ctx.now.strftime('%Y-%m-%d')}-{ctx.slot}"
        table = [{
            "venue": r.venue, "label": ctx.labels.get(r.venue, ""), "ticker": r.ticker,
            "title": r.title, "subtitle": r.subtitle, "category": r.category,
            "prev": r.prev_24h_price, "now": r.yes_price, "delta": r.change_24h,
            "volume_24h": r.volume_24h, "close_time": r.close_time, "url": r.url,
        } for r in rows[: int(ctx.th("movers_table_n", 20))]]
        return Post(
            id=pid, slot=ctx.slot, shape=self.name,
            title=f"Biggest odds swings — {ctx.now.strftime('%b %-d, %Y')}",
            text=text, published_at=ctx.now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            tags=tags, rows=table, extra={"record": record, "chart_kind": "movers"},
        )
