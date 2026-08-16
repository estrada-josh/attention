"""Kalshi public market data (no auth).

Verified 2026-08-16:
  GET /trade-api/v2/markets?status=open&mve_filter=exclude&limit=1000&cursor=
      -> {"cursor": str, "markets": [...]}; ~80k markets, ~80 pages, ~10 s.
      The list is ordered by open_time DESC, so the last page holds the
      long-dated election markets. Fields: ticker, event_ticker, title,
      yes_sub_title, last_price_dollars, previous_price_dollars (0 for markets
      younger than 24h), volume_24h_fp, volume_fp, open_interest_fp,
      liquidity_dollars, close_time, open_time, status ("active"),
      result ("" until settled).
  GET /trade-api/v2/markets?status=settled&mve_filter=exclude&limit=1000
      -> status "finalized"; result yes/no; settlement_ts and open_time present.
      The list is NOT sorted by close_time (measured 2026-08-16: 342 close_time
      inversions in one page), so pagination must not stop on the last item of
      a page. Pass min_close_ts=<epoch> to filter server-side instead.
  GET /trade-api/v2/markets?tickers=A,B,C&limit=100
      -> the named markets only (verified 2026-08-16). Used to fetch watchlist
      markets that the paged list scan missed.
  GET /trade-api/v2/series?limit=1000 -> all series with category (one call).
      Market category = series category; series = event_ticker before "-".
Without mve_filter=exclude, ~29k multivariate parlay legs flood the list.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from ..model import MarketRow
from .base import Source

log = logging.getLogger("engine.sources.kalshi")
BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _f(x, default=None):
    try:
        if x in (None, ""):
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


class KalshiSource(Source):
    name = "kalshi"
    label = "K"

    def __init__(self, cfg: dict, http):
        super().__init__(cfg, http)
        self.base = cfg.get("base_url", BASE)
        self.page_sleep = float(cfg.get("page_sleep", 0.3))
        self.max_pages = int(cfg.get("max_pages", 150))
        self._categories: dict[str, str] | None = None
        self._event_titles: dict[str, str] = {}

    # ------------------------------------------------------------ helpers
    def _paginate(self, path: str, params: dict, key: str):
        cursor = ""
        pages = 0
        while True:
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            d = self.http.get_json(f"{self.base}{path}", p)
            items = d.get(key) or []
            pages += 1
            for it in items:
                yield it
            cursor = d.get("cursor") or ""
            if not cursor:
                return
            if pages >= self.max_pages:
                log.warning("kalshi: page cap %d hit on %s %s; the list is truncated",
                            self.max_pages, path, params.get("status", ""))
                return
            time.sleep(self.page_sleep)

    def event_categories(self) -> dict[str, str]:
        """series_ticker -> category. One request: GET /series?limit=1000
        returns all ~13k series (verified 2026-08-16, no cursor)."""
        if self._categories is None:
            cats: dict[str, str] = {}
            d = self.http.get_json(f"{self.base}/series", {"limit": 1000})
            for e in d.get("series") or []:
                cats[e["ticker"]] = e.get("category") or ""
            self._categories = cats
            log.info("kalshi: %d series with categories", len(cats))
        return self._categories

    @staticmethod
    def series_of(event_ticker: str) -> str:
        return (event_ticker or "").split("-", 1)[0]

    def _row(self, m: dict, status: str) -> MarketRow:
        cats = self._categories or {}
        title = m.get("title") or ""
        sub = m.get("yes_sub_title") or ""
        if sub and sub.strip().lower() in title.strip().lower():
            sub = ""
        return MarketRow(
            venue=self.name,
            ticker=m["ticker"],
            title=title,
            subtitle=sub,
            category=cats.get(self.series_of(m.get("event_ticker", "")), ""),
            yes_price=_f(m.get("last_price_dollars")),
            prev_24h_price=None,     # filled by the engine from snapshots
            change_24h=None,
            volume_24h=_f(m.get("volume_24h_fp"), 0.0),
            open_interest=_f(m.get("open_interest_fp")),
            liquidity=_f(m.get("liquidity_dollars")),
            close_time=m.get("close_time"),
            open_time=m.get("open_time"),
            url=f"https://kalshi.com/markets/{m.get('event_ticker', '').lower()}",
            status=status,
            result=(m.get("result") or None) if status == "settled" else None,
            settled_at=m.get("settlement_ts") if status == "settled" else None,
            event_ticker=m.get("event_ticker", ""),
        )

    def history_price(self, row: MarketRow, when: datetime) -> float | None:
        """Last hourly candle close at or before `when` (window: 48h before).
        GET /series/{series}/markets/{ticker}/candlesticks?start_ts&end_ts&period_interval=60
        (verified 2026-08-16: candlesticks[].price.close_dollars, end_period_ts)."""
        series = self.series_of(row.event_ticker)
        if not series:
            return None
        end = int(when.timestamp())
        try:
            d = self.http.get_json(f"{self.base}/series/{series}/markets/{row.ticker}/candlesticks",
                                   {"start_ts": end - 48 * 3600, "end_ts": end, "period_interval": 60}, tries=2)
        except Exception as e:  # noqa: BLE001
            log.info("kalshi candlesticks failed for %s: %s", row.ticker, e)
            return None
        best = None
        for c in d.get("candlesticks") or []:
            ts = int(c.get("end_period_ts") or 0)
            close = _f((c.get("price") or {}).get("close_dollars"))
            if close is None or ts > end + 3600:
                continue
            if best is None or ts >= best[0]:
                best = (ts, close)
        return best[1] if best else None

    # ------------------------------------------------------------ contract
    def fetch_open(self) -> list[MarketRow]:
        self.event_categories()
        rows = []
        seen: set[str] = set()
        for m in self._paginate("/markets", {"status": "open", "mve_filter": "exclude", "limit": 1000}, "markets"):
            if m["ticker"] in seen:
                continue
            seen.add(m["ticker"])
            r = self._row(m, "open")
            r.extra_prev = _f(m.get("previous_price_dollars"))          # type: ignore[attr-defined]
            rows.append(r)
        log.info("kalshi: %d open markets", len(rows))
        return rows

    def fetch_settled(self, since: datetime) -> list[MarketRow]:
        """Every settled market with close_time >= `since`.

        The settled list is not sorted by close_time, so the walk cannot stop on
        an old item. min_close_ts filters server-side; the client-side compare
        below is the defense in depth.
        """
        self.event_categories()
        since = since.astimezone(timezone.utc)
        cutoff = since.isoformat().replace("+00:00", "Z")
        params = {"status": "settled", "mve_filter": "exclude", "limit": 1000,
                  "min_close_ts": int(since.timestamp())}
        rows = []
        for m in self._paginate("/markets", params, "markets"):
            if (m.get("close_time") or "") < cutoff:
                continue
            if m.get("result") not in ("yes", "no"):
                continue
            rows.append(self._row(m, "settled"))
        log.info("kalshi: %d settled since %s", len(rows), cutoff)
        return rows

    def fetch_by_ids(self, ids: list[str]) -> list[MarketRow]:
        """The named markets, 100 tickers per request."""
        self.event_categories()
        rows = []
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            d = self.http.get_json(f"{self.base}/markets",
                                   {"tickers": ",".join(chunk), "limit": 100}, tries=2)
            for m in d.get("markets") or []:
                r = self._row(m, "open")
                r.extra_prev = _f(m.get("previous_price_dollars"))       # type: ignore[attr-defined]
                rows.append(r)
            if i + 100 < len(ids):
                time.sleep(self.page_sleep)
        log.info("kalshi: fetched %d/%d markets by ticker", len(rows), len(ids))
        return rows
