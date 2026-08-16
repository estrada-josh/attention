"""Polymarket Gamma public API (no auth).

Verified 2026-08-16:
  GET https://gamma-api.polymarket.com/markets?closed=false&active=true
      &order=volume24hr&ascending=false&limit=100&offset=N
      -> list of markets; limit caps at 100 per page; offset > 2000 -> 422
      ("use /markets/keyset for deeper pagination"), so ~2,100 markets max.
      Fields: question, slug, outcomes (JSON string), outcomePrices (JSON
      string), oneDayPriceChange, oneWeekPriceChange, volume24hr,
      lastTradePrice, liquidityNum, endDate, startDate, conditionId,
      sportsMarketType, gameStartTime, events[{slug,title}].
  GET .../markets?closed=true&order=closedTime&ascending=false&limit=100
      -> resolved markets newest first; umaResolutionStatus="resolved";
      outcomePrices ["1","0"] means outcomes[0] won.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from ..model import MarketRow
from .base import Source

log = logging.getLogger("engine.sources.polymarket")
BASE = "https://gamma-api.polymarket.com"


def _f(x, default=None):
    try:
        if x in (None, ""):
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _jlist(s):
    if isinstance(s, list):
        return s
    try:
        return json.loads(s or "[]")
    except ValueError:
        return []


class PolymarketSource(Source):
    name = "polymarket"
    label = "P"

    def __init__(self, cfg: dict, http):
        super().__init__(cfg, http)
        self.base = cfg.get("base_url", BASE)
        self.page_sleep = float(cfg.get("page_sleep", 0.4))
        self.max_pages = int(cfg.get("max_pages", 21))   # offset > 2000 -> 422
        self.min_volume_24h_stop = float(cfg.get("min_volume_24h_stop", 500))
        self._event_cat: dict[str, str] = {}      # event slug -> category (from event tags)
        self._events_loaded = False

    SPORT_TAGS = {"sports", "esports", "games", "soccer", "nfl", "nba", "mlb", "nhl", "tennis", "mma", "ufc", "boxing", "golf", "f1", "cricket"}

    @staticmethod
    def _cat_from_tags(tags: list[dict]) -> str:
        labels = [str(t.get("label") or "") for t in tags or []]
        if any(l.lower() in PolymarketSource.SPORT_TAGS for l in labels):
            return "Sports"
        return labels[0] if labels else ""

    def _load_open_event_categories(self) -> None:
        """GET /events?closed=false&order=volume24hr&limit=100&offset=N -> tags per event
        (verified 2026-08-16: events[].tags[].label; nested market payloads carry no tags)."""
        if self._events_loaded:
            return
        offset = 0
        for _ in range(self.max_pages):
            try:
                d = self.http.get_json(f"{self.base}/events", {"closed": "false", "order": "volume24hr",
                                                               "ascending": "false", "limit": 100, "offset": offset}, tries=3)
            except Exception as e:  # noqa: BLE001
                log.warning("polymarket events page failed at offset %d: %s", offset, e)
                break
            if not d:
                break
            for e in d:
                if e.get("slug"):
                    self._event_cat[e["slug"]] = self._cat_from_tags(e.get("tags") or [])
            offset += len(d)
            if len(d) < 100:
                break
            time.sleep(self.page_sleep)
        self._events_loaded = True
        log.info("polymarket: %d open events with tag categories", len(self._event_cat))

    def _event_category(self, slug: str) -> str:
        if not slug:
            return ""
        if slug not in self._event_cat:
            try:
                d = self.http.get_json(f"{self.base}/events", {"slug": slug}, tries=2)
                self._event_cat[slug] = self._cat_from_tags((d[0].get("tags") if d else []) or [])
            except Exception:  # noqa: BLE001
                self._event_cat[slug] = ""
        return self._event_cat[slug]

    def _row(self, m: dict, status: str) -> MarketRow:
        outcomes = _jlist(m.get("outcomes"))
        prices = [_f(p) for p in _jlist(m.get("outcomePrices"))]
        yes_price = prices[0] if prices else None
        first = outcomes[0] if outcomes else "Yes"
        subtitle = "" if str(first).lower() == "yes" else str(first)
        events = m.get("events") or []
        ev_slug = events[0].get("slug") if events else None
        url = f"https://polymarket.com/event/{ev_slug}" if ev_slug else f"https://polymarket.com/market/{m.get('slug')}"
        is_sport = bool(m.get("sportsMarketType") or m.get("gameStartTime"))
        result = None
        if status == "settled" and prices and len(prices) >= 2:
            if prices[0] >= 0.99:
                result = "yes"
            elif prices[0] <= 0.01:
                result = "no"
        change = _f(m.get("oneDayPriceChange"))
        tokens = _jlist(m.get("clobTokenIds"))
        row = MarketRow(
            venue=self.name,
            ticker=m.get("slug") or str(m.get("id")),
            title=m.get("question") or "",
            subtitle=subtitle,
            category="Sports" if is_sport else self._event_cat.get(ev_slug or "", ""),
            yes_price=yes_price,
            prev_24h_price=(yes_price - change) if (yes_price is not None and change is not None and status == "open") else None,
            change_24h=change if status == "open" else None,
            volume_24h=_f(m.get("volume24hr"), 0.0),
            open_interest=None,
            liquidity=_f(m.get("liquidityNum")),
            close_time=m.get("endDate"),
            url=url,
            status=status,
            result=result,
            settled_at=(m.get("closedTime") or "").replace(" ", "T").replace("+00", "Z") if status == "settled" else None,
            event_ticker=ev_slug or "",
        )
        row.extra_token = tokens[0] if tokens else None   # type: ignore[attr-defined]
        return row

    def history_price(self, row: MarketRow, when: datetime) -> float | None:
        """Last point at or before `when` from CLOB price history (48h window).
        GET https://clob.polymarket.com/prices-history?market=<tokenId>&startTs&endTs&fidelity=60
        (verified 2026-08-16: {"history":[{"t":unix,"p":price}]})."""
        token = getattr(row, "extra_token", None)
        if not token:
            return None
        end = int(when.timestamp())
        try:
            d = self.http.get_json("https://clob.polymarket.com/prices-history",
                                   {"market": token, "startTs": end - 48 * 3600, "endTs": end, "fidelity": 60}, tries=2)
        except Exception as e:  # noqa: BLE001
            log.info("polymarket prices-history failed for %s: %s", row.ticker, e)
            return None
        best = None
        for pt in d.get("history") or []:
            t, p = int(pt.get("t") or 0), _f(pt.get("p"))
            if p is None or t > end + 3600:
                continue
            if best is None or t >= best[0]:
                best = (t, p)
        return best[1] if best else None

    def fetch_open(self) -> list[MarketRow]:
        self._load_open_event_categories()
        rows = []
        seen: set[str] = set()
        offset = 0
        for page in range(self.max_pages):
            d = self.http.get_json(f"{self.base}/markets", {
                "closed": "false", "active": "true", "order": "volume24hr",
                "ascending": "false", "limit": 100, "offset": offset})
            if not d:
                break
            for m in d:
                r = self._row(m, "open")
                if r.ticker in seen:
                    continue
                seen.add(r.ticker)
                rows.append(r)
            offset += len(d)
            if len(d) < 100 or _f(d[-1].get("volume24hr"), 0.0) < self.min_volume_24h_stop:
                break
            time.sleep(self.page_sleep)
        log.info("polymarket: %d open markets (vol24h >= %s)", len(rows), self.min_volume_24h_stop)
        return rows

    def fetch_settled(self, since: datetime) -> list[MarketRow]:
        cutoff = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        rows = []
        offset = 0
        for page in range(self.max_pages):
            d = self.http.get_json(f"{self.base}/markets", {
                "closed": "true", "order": "closedTime", "ascending": "false",
                "limit": 100, "offset": offset})
            if not d:
                break
            stop = False
            for m in d:
                ct = (m.get("closedTime") or "").replace(" ", "T")
                if ct and ct < cutoff:
                    stop = True
                    break
                if (m.get("umaResolutionStatus") or "") != "resolved":
                    continue
                r = self._row(m, "settled")
                if r.result and r.ticker not in {x.ticker for x in rows}:
                    rows.append(r)
            if stop or len(d) < 100:
                break
            offset += len(d)
            time.sleep(self.page_sleep)
        # categories for the settled markets that could be posted (bounded lookups)
        budget = int(self.cfg.get("settled_category_lookups", 60))
        for r in sorted(rows, key=lambda x: x.volume_24h or 0, reverse=True):
            if budget <= 0:
                break
            if r.category == "" and r.event_ticker:
                r.category = self._event_category(r.event_ticker)
                budget -= 1
        log.info("polymarket: %d settled since %s", len(rows), cutoff)
        return rows
