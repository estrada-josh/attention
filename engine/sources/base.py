"""Source contract. A source returns normalized MarketRow lists."""
from __future__ import annotations

from datetime import datetime

from ..model import MarketRow


class Source:
    name: str = "base"          # venue code used in MarketRow.venue
    label: str = "?"            # one-letter label for post text

    def __init__(self, cfg: dict, http):
        self.cfg = cfg
        self.http = http

    def fetch_open(self) -> list[MarketRow]:
        """All open markets worth tracking (already filtered for junk)."""
        raise NotImplementedError

    def fetch_settled(self, since: datetime) -> list[MarketRow]:
        """Markets that settled/closed at or after `since`."""
        raise NotImplementedError

    def market_url(self, row: MarketRow) -> str:
        return row.url

    def history_price(self, row: MarketRow, when: datetime) -> float | None:
        """YES price at (or just before) `when` from the venue's history API.
        Optional; used when our own snapshots have no reference price."""
        return None
