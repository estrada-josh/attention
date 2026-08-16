"""Settled & surprised: what resolved in the last 24h, upsets first."""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Optional

from ..model import Post
from .base import Context, Shape, cents, clip, fit_lines, money


class SettledShape(Shape):
    name = "settled"

    def build(self, ctx: Context) -> Optional[Post]:
        excl = set(ctx.th("exclude_categories", []) or [])
        min_vol = float(ctx.th("settled_min_volume", 10000))
        upset_lo = float(ctx.th("upset_yes_max_price", 0.15))
        upset_hi = float(ctx.th("upset_no_min_price", 0.85))
        title_rx = re.compile(ctx.th("exclude_title_regex", "$^"), re.I)
        rows = [r for r in ctx.settled_rows if r.result in ("yes", "no") and r.category not in excl and not title_rx.search(r.title)]
        # price_24h_before is attached by the engine as attribute p24 (may be None)
        def p24(r):
            return getattr(r, "p24", None)
        rows = [r for r in rows if p24(r) is not None and (r.volume_24h or 0) >= min_vol]
        # The settled window is 26h, so a market that closed late yesterday is in
        # today's rows too. Never feature the same market on two days.
        today = ctx.now.strftime("%Y-%m-%d")
        keep_from = (ctx.now - timedelta(days=2)).strftime("%Y-%m-%d")
        featured = {k: d for k, d in (ctx.state.get("settled_featured") or {}).items() if d >= keep_from}
        ctx.state["settled_featured"] = featured
        rows = [r for r in rows if featured.get(f"{r.venue}:{r.ticker}", today) == today]
        if not rows:
            return None
        upsets = [r for r in rows if (r.result == "yes" and p24(r) <= upset_lo) or (r.result == "no" and p24(r) >= upset_hi)]
        upsets.sort(key=lambda r: (abs((1.0 if r.result == "yes" else 0.0) - p24(r)), r.volume_24h or 0), reverse=True)
        others = [r for r in rows if r not in upsets]
        others.sort(key=lambda r: r.volume_24h or 0, reverse=True)
        top_n = int(ctx.th("settled_top_n", 4))
        picked = (upsets + others)[:top_n]
        if not picked:
            return None
        for r in picked:
            featured[f"{r.venue}:{r.ticker}"] = today
        # rarity counter for upsets over 30 days (from resolutions.csv)
        cutoff = (ctx.now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        n_upsets_30 = 0
        for res in ctx.store.load_resolutions():
            try:
                p = float(res.get("price_24h_before") or "")
            except ValueError:
                continue
            if (res.get("settled_at") or "") < cutoff:
                continue
            if (res["result"] == "yes" and p <= upset_lo) or (res["result"] == "no" and p >= upset_hi):
                n_upsets_30 += 1
        header = "Settled in the last 24h"

        def make_lines(clip_n: int) -> list[str]:
            out = []
            for r in picked:
                label = ctx.labels.get(r.venue, r.venue[:1].upper())
                name = clip(r.title + (f" — {r.subtitle}" if r.subtitle else ""), clip_n)
                base = f"{label} · {name} → {r.result.upper()} · was {cents(p24(r))} ~24h before · {money(r.volume_24h)}"
                if r in upsets:
                    base = "UPSET · " + base
                out.append(base)
            return out
        foot = []
        if upsets:
            foot.append(f"Upsets (≤{round(upset_lo*100)}¢ or ≥{round(upset_hi*100)}¢ the day before) in 30 days: {n_upsets_30}")
        tags = list(ctx.audience.tags[:1])
        for v in sorted({r.venue for r in picked}):
            t = (ctx.th("venue_tags", {}) or {}).get(v)
            if t and len(tags) < 3:
                tags.append(t)
        foot.append(" ".join(f"#{t}" for t in tags))
        text = fit_lines(header, make_lines, "\n".join(foot), clips=(44, 34, 26), what=f"settled {ctx.slot}")
        pid = f"{ctx.now.strftime('%Y-%m-%d')}-{ctx.slot}"
        table = [{
            "venue": r.venue, "label": ctx.labels.get(r.venue, ""), "ticker": r.ticker, "title": r.title,
            "subtitle": r.subtitle, "category": r.category, "result": r.result, "p24": p24(r),
            "volume_24h": r.volume_24h, "settled_at": r.settled_at, "url": r.url, "upset": r in upsets,
        } for r in (upsets + others)[: int(ctx.th("settled_table_n", 20))]]
        return Post(
            id=pid, slot=ctx.slot, shape=self.name,
            title=f"Settled & surprised — {ctx.now.strftime('%b %-d, %Y')}",
            text=text, published_at=ctx.now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            tags=tags, rows=table, extra={"n_upsets": len(upsets), "n_upsets_30d": n_upsets_30, "chart_kind": "settled"},
        )
