"""Shape contract. A shape turns a Context into one Post (or None = skip)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..model import MarketRow, Post


@dataclass
class Context:
    now: datetime
    audience: object                     # engine.config.Audience
    store: object                        # engine.state.Store
    open_rows: list[MarketRow]
    settled_rows: list[MarketRow]        # settled since ~24h, enriched
    labels: dict[str, str]               # venue -> one-letter label
    slot: str
    state: dict
    extra: dict = field(default_factory=dict)

    def th(self, key: str, default=None):
        """Threshold lookup: shape-level override, then audience-level, then default."""
        shape_cfg = self.extra.get("shape_cfg") or {}
        if key in shape_cfg:
            return shape_cfg[key]
        return self.audience.thresholds.get(key, default)


class Shape:
    name = "base"

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def build(self, ctx: Context) -> Optional[Post]:
        raise NotImplementedError


# ------------------------------------------------------------ text helpers
def cents(p: Optional[float]) -> str:
    if p is None:
        return "—"
    return f"{round(p * 100):d}¢"


def pts(delta: Optional[float]) -> str:
    if delta is None:
        return "—"
    d = round(delta * 100)
    return f"{d:+d}"


def money(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if v >= 1_000_000:
        return f"${v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"${v / 1_000:.0f}k"
    return f"${v:.0f}"


def clip(s: str, n: int) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def fit_text(header: str, lines: list[str], footer: str, limit: int = 280) -> str:
    """Join header + as many lines as fit + footer, staying under `limit` chars."""
    keep = list(lines)
    while True:
        text = "\n".join([header, *keep, footer]).strip()
        if len(text) <= limit or not keep:
            return text
        keep.pop()


def score_move(delta: float, volume: float) -> float:
    return abs(delta) * math.log10(max(volume, 10.0))
