"""Core data types shared by sources, shapes, and channels."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional


@dataclass
class MarketRow:
    """One market at one moment, normalized across venues.

    Prices are probabilities in [0, 1] for the YES side. Money is USD.
    A settled market carries `result` ("yes"/"no") and `settled_at`.
    """
    venue: str                    # short code: "kalshi", "polymarket"
    ticker: str                   # venue-unique id
    title: str                    # human title (question)
    subtitle: str = ""            # strike / outcome label, if any
    category: str = ""
    yes_price: Optional[float] = None
    prev_24h_price: Optional[float] = None
    change_24h: Optional[float] = None      # yes_price - prev_24h_price
    volume_24h: Optional[float] = None
    open_interest: Optional[float] = None
    liquidity: Optional[float] = None
    close_time: Optional[str] = None        # ISO-8601 UTC
    open_time: Optional[str] = None         # ISO-8601 UTC; when the market opened
    url: str = ""
    status: str = "open"                    # open | settled
    result: Optional[str] = None            # yes | no | None
    settled_at: Optional[str] = None
    event_ticker: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "MarketRow":
        keys = MarketRow.__dataclass_fields__.keys()
        clean = {}
        for k in keys:
            v = d.get(k)
            if v == "":
                v = None if k not in ("subtitle", "category", "url", "event_ticker", "title") else ""
            clean[k] = v
        for k in ("yes_price", "prev_24h_price", "change_24h", "volume_24h",
                  "open_interest", "liquidity"):
            if clean.get(k) not in (None, ""):
                clean[k] = float(clean[k])
        clean["status"] = clean.get("status") or "open"
        return MarketRow(**clean)


@dataclass
class Post:
    """One publishable unit produced by a shape.

    `text` is the social copy (<= 280 graphemes). `rows` is the full table
    for the site page. `chart` is a PNG path relative to the site root.
    """
    id: str                       # e.g. "2026-08-16-am"
    slot: str                     # am | pm | board | sunday | ...
    shape: str                    # shape name
    title: str
    text: str
    published_at: str             # ISO-8601 UTC
    tags: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    chart: Optional[str] = None
    chart_alt: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def utcnow_iso() -> str:
    from datetime import timezone
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
