"""Calibration scorecard: how well did prices the day before predict outcomes?"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Optional

from ..model import Post
from .base import Context, Shape, fit_text


def brier(rows) -> Optional[float]:
    if not rows:
        return None
    return sum((p - (1.0 if res == "yes" else 0.0)) ** 2 for p, res in rows) / len(rows)


class ScorecardShape(Shape):
    name = "scorecard"

    def build(self, ctx: Context) -> Optional[Post]:
        days = int(self.cfg.get("window_days", 7))
        min_n = int(self.cfg.get("min_resolved", 20))
        excl = set(ctx.th("exclude_categories", []) or [])
        cutoff = (ctx.now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        by_venue_week: dict[str, list] = defaultdict(list)
        by_venue_all: dict[str, list] = defaultdict(list)
        buckets_week = defaultdict(lambda: [0, 0])   # bucket -> [n, yes]
        for res in ctx.store.load_resolutions():
            try:
                p = float(res.get("price_24h_before") or "")
            except ValueError:
                continue
            if res.get("result") not in ("yes", "no"):
                continue
            pair = (p, res["result"])
            by_venue_all[res["venue"]].append(pair)
            if (res.get("settled_at") or "") >= cutoff:
                by_venue_week[res["venue"]].append(pair)
                b = min(int(p * 10), 9)
                buckets_week[b][0] += 1
                buckets_week[b][1] += 1 if res["result"] == "yes" else 0
        n_week = sum(len(v) for v in by_venue_week.values())
        if n_week < min_n:
            return None
        week_no = ctx.now.isocalendar()[1]
        header = f"Calibration scorecard, week {week_no}"
        lines = [
            "Resolved: " + ", ".join(f"{len(v)} {ctx.labels.get(k, k)}" for k, v in sorted(by_venue_week.items())),
        ]
        hi = buckets_week[9]
        lo = buckets_week[0]
        if hi[0]:
            lines.append(f"Priced ≥90¢ the day before: {hi[1]}/{hi[0]} resolved YES")
        if lo[0]:
            lines.append(f"Priced ≤10¢: {lo[1]}/{lo[0]} resolved YES")
        wk = " · ".join(f"{ctx.labels.get(k, k)} {brier(v):.3f}" for k, v in sorted(by_venue_week.items()) if brier(v) is not None)
        run = " · ".join(f"{ctx.labels.get(k, k)} {brier(v):.3f}" for k, v in sorted(by_venue_all.items()) if brier(v) is not None)
        lines.append(f"Weekly Brier: {wk} (running {run})")
        lines.append(f"{ctx.audience.domain}/calibration")
        tags = list(ctx.audience.tags[:1])
        text = fit_text(header, lines, " ".join(f"#{t}" for t in tags))
        pid = f"{ctx.now.strftime('%Y-%m-%d')}-{ctx.slot}"
        table = [{"bucket": f"{b*10}-{b*10+10}¢", "n": v[0], "yes": v[1], "rate": (v[1] / v[0]) if v[0] else None}
                 for b, v in sorted(buckets_week.items())]
        return Post(
            id=pid, slot=ctx.slot, shape=self.name,
            title=f"Calibration scorecard — week {week_no}, {ctx.now.year}",
            text=text, published_at=ctx.now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            tags=tags, rows=table,
            extra={"chart_kind": "scorecard", "brier_week": {k: brier(v) for k, v in by_venue_week.items()},
                   "brier_all": {k: brier(v) for k, v in by_venue_all.items()}, "n_week": n_week},
        )
