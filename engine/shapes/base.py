"""Shape contract. A shape turns a Context into one Post (or None = skip)."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

from ..model import MarketRow, Post

log = logging.getLogger("engine.shapes")


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


def delta_pts(prev: Optional[float], now: Optional[float]) -> str:
    """The move in points, computed from the ROUNDED endpoints.

    cents() rounds each price before we print it, so subtracting the raw prices
    can disagree with the two numbers on screen (5¢ -> 92¢ printed as +88).
    Rounding first and subtracting keeps the text self-consistent.
    """
    if prev is None or now is None:
        return "—"
    d = round(now * 100) - round(prev * 100)
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


def fit_text(header: str, lines: list[str], footer: str, limit: int = 280,
             what: str = "post") -> str:
    """Join header + as many lines as fit + footer, staying under `limit` chars.

    Dropping a line hides a configured item, so the drop is logged.
    """
    keep = list(lines)
    while True:
        text = "\n".join([header, *keep, footer]).strip()
        if len(text) <= limit or not keep:
            if len(keep) < len(lines):
                log.warning("%s: dropped %d of %d lines to fit %d chars",
                            what, len(lines) - len(keep), len(lines), limit)
            return text
        keep.pop()


def fit_lines(header: str, make_lines: Callable[[int], list[str]], footer: str,
              clips: tuple[int, ...] = (48, 36, 28), limit: int = 280,
              what: str = "post") -> str:
    """Same as fit_text, but shorten the titles before dropping whole lines.

    `make_lines(clip)` rebuilds every line with titles clipped to `clip` chars.
    A shorter title still names the market; a dropped line hides it entirely.
    Of the clips that keep the most lines, this returns the longest one, so a
    shorter title is only used when it buys another line.
    """
    best_text, best_kept, total = "", -1, 0
    for clip_n in clips:
        lines = make_lines(clip_n)
        total = len(lines)
        keep = list(lines)
        text = "\n".join([header, *keep, footer]).strip()
        while keep and len(text) > limit:
            keep.pop()
            text = "\n".join([header, *keep, footer]).strip()
        if len(keep) > best_kept:
            best_text, best_kept = text, len(keep)
        if best_kept == total:
            break                    # a shorter title cannot buy another line
    if best_kept < total:
        log.warning("%s: dropped %d of %d lines to fit %d chars",
                    what, total - best_kept, total, limit)
    return best_text


def score_move(delta: float, volume: float) -> float:
    return abs(delta) * math.log10(max(volume, 10.0))
