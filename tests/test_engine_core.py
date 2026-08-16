"""Zero-network tests for the engine core: slots, breakers, sources, text.

Every source here is a fake. Nothing in this file touches the network or any
path outside tmp_path.
"""
import gzip
import json
from datetime import datetime, timedelta, timezone

import pytest

from engine import healthcheck as hc_mod
from engine import run as run_mod
from engine.config import Audience
from engine.model import MarketRow
from engine.run import resolve_slot, run, watch_ids
from engine.shapes import Context, build_shape
from engine.shapes.base import delta_pts, fit_lines, fit_text
from engine.sources.kalshi import KalshiSource
from engine.sources.polymarket import PolymarketSource
from engine.state import Store

NOW = datetime(2026, 8, 17, 13, 20, tzinfo=timezone.utc)          # a Monday
SHAPES = [
    {"slot": "am", "type": "movers", "at": "13:17"},
    {"slot": "board", "type": "watchlist", "at": "17:17", "min_found": 1,
     "watch": [{"name": "Senate", "match": [{"venue": "k", "ticker": "SEN-D"}]}]},
    {"slot": "pm", "type": "settled", "at": "22:17"},
]


def _aud(tmp_path, shapes=None, **thresholds) -> Audience:
    """An audience whose files all live under tmp_path."""
    class TmpAudience(Audience):
        @property
        def dir(self):
            return tmp_path

    th = {"exclude_categories": ["Sports"], "store_exclude_categories": ["Sports"],
          "min_volume_24h": {"k": 5000}, "movers_min_points": 8, "venue_tags": {"k": "Kalshi"},
          "settled_min_volume": 10000, "snapshot_min_volume_24h": 250,
          "snapshot_min_open_interest": 1500, "resolution_min_volume_24h": 100,
          "settled_min_lifetime_hours": 30, "breaker_failures": 3, "breaker_cooldown_hours": 12}
    th.update(thresholds)
    return TmpAudience(name="t", display_name="T (bot)", domain="t.example.com",
                       site_url="https://t.example.com", description="d", contact_email="t@example.com",
                       repo="x/y", site_root="audiences/t/site", tags=["PredictionMarkets"],
                       sources=[{"type": "k"}], shapes=shapes if shapes is not None else SHAPES,
                       channels=[], thresholds=th, raw={})


def _open(ticker, price=0.41, prev=0.12, vol=20000, cat="Politics", **kw):
    return MarketRow(venue="k", ticker=ticker, title=f"Will {ticker} happen?", yes_price=price,
                     prev_24h_price=prev, volume_24h=vol, category=cat, url="https://x/y",
                     close_time=(NOW + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"), **kw)


def _settled(ticker, lifetime_h=240, vol=50000, cat="Politics", title=None):
    close = NOW - timedelta(hours=2)
    return MarketRow(venue="k", ticker=ticker, title=title or f"Will {ticker} happen?", result="yes",
                     volume_24h=vol, category=cat, yes_price=0.99, status="settled", url="https://x/y",
                     close_time=close.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     settled_at=close.strftime("%Y-%m-%dT%H:%M:%SZ"),
                     open_time=(close - timedelta(hours=lifetime_h)).strftime("%Y-%m-%dT%H:%M:%SZ"))


class FakeSource:
    """One venue, canned answers, counting every call."""
    name = "k"
    label = "K"

    def __init__(self, open_rows=(), settled=(), by_id=None, fail=False):
        self.open_rows = list(open_rows)
        self.settled = list(settled)
        self.by_id = by_id or {}
        self.fail = fail
        self.id_calls: list[list[str]] = []
        self.history_calls: list[str] = []

    def fetch_open(self):
        if self.fail:
            raise RuntimeError("venue down")
        return list(self.open_rows)

    def fetch_settled(self, since):
        return list(self.settled)

    def fetch_by_ids(self, ids):
        self.id_calls.append(list(ids))
        return [self.by_id[i] for i in ids if i in self.by_id]

    def history_price(self, row, when):
        self.history_calls.append(row.ticker)
        return 0.4


def _patch_sources(monkeypatch, *sources):
    monkeypatch.setattr(run_mod, "build_sources", lambda cfgs, http: list(sources))


# ---------------------------------------------------------------- resolve_slot
def test_resolve_slot_picks_the_earliest_slot_that_is_still_open(tmp_path):
    aud = _aud(tmp_path)
    state = {"slots_done": {}}
    # 17:17: 'am' is 4h late (inside the 240 min grace) and 'board' is due now,
    # so the earliest open slot wins and 'am' is caught up first
    at_board = NOW.replace(hour=17, minute=17)
    assert resolve_slot(aud, at_board, "auto", state)["slot"] == "am"
    state["slots_done"]["2026-08-17-am"] = "done"
    assert resolve_slot(aud, at_board, "auto", state)["slot"] == "board"


def test_resolve_slot_marks_a_slot_past_the_grace_window_as_skipped(tmp_path):
    aud = _aud(tmp_path, slot_grace_minutes=90)
    state = {"slots_done": {}}
    at_board = NOW.replace(hour=17, minute=17)
    assert resolve_slot(aud, at_board, "auto", state)["slot"] == "board"
    assert state["slots_done"]["2026-08-17-am"].endswith("(skipped)")


def test_resolve_slot_rejects_an_unknown_explicit_slot(tmp_path):
    with pytest.raises(SystemExit):
        resolve_slot(_aud(tmp_path), NOW, "moring", {})


def test_watch_ids_collects_every_venue_ticker(tmp_path):
    assert watch_ids(_aud(tmp_path)) == {"k": ["SEN-D"]}


# ---------------------------------------------------------------- run: slots
def test_slot_stays_open_when_every_source_failed(tmp_path, monkeypatch):
    aud = _aud(tmp_path)
    src = FakeSource(fail=True)
    _patch_sources(monkeypatch, src)
    assert run(aud, slot="am", now=NOW) is None
    state = Store(tmp_path / "data").load_state()
    assert "2026-08-17-am" not in state["slots_done"]
    assert state["last_run"]["no_post_reason"] == "sources failed: k"
    assert state["breakers"]["k"]["failures"] == 1


def test_slot_closes_with_no_post_when_nothing_qualified(tmp_path, monkeypatch):
    aud = _aud(tmp_path)
    _patch_sources(monkeypatch, FakeSource(open_rows=[_open("A")]))   # 1 row < movers_min_rows
    assert run(aud, slot="am", now=NOW) is None
    state = Store(tmp_path / "data").load_state()
    assert state["slots_done"]["2026-08-17-am"].endswith("(no post)")
    assert state["last_run"]["no_post_reason"] == "nothing qualified"


def test_a_post_from_one_venue_records_the_missing_venue(tmp_path, monkeypatch):
    aud = _aud(tmp_path)
    good = FakeSource(open_rows=[_open("A"), _open("B", price=0.31, prev=0.44)])
    bad = FakeSource(fail=True)
    bad.name = "p"
    _patch_sources(monkeypatch, good, bad)
    post = run(aud, slot="am", now=NOW)
    assert post is not None
    state = Store(tmp_path / "data").load_state()
    assert state["slots_done"]["2026-08-17-am"].endswith("(missing: p)")


# ---------------------------------------------------------------- run: breaker
def test_breaker_trips_then_goes_half_open_after_the_cooldown(tmp_path, monkeypatch):
    aud = _aud(tmp_path)
    _patch_sources(monkeypatch, FakeSource(fail=True))
    for i in range(3):
        run(aud, slot="am", now=NOW + timedelta(minutes=i), force=True)
    state = Store(tmp_path / "data").load_state()
    assert state["breakers"]["k"]["disabled"] is True and state["breakers"]["k"]["disabled_at"]

    # inside the cooldown: the source is skipped, so fetch_open is never called
    cold = FakeSource(fail=True)
    _patch_sources(monkeypatch, cold)
    run(aud, slot="am", now=NOW + timedelta(hours=2), force=True)
    assert Store(tmp_path / "data").load_state()["breakers"]["k"]["failures"] == 3

    # after the cooldown: tried again, and a success clears the breaker
    healthy = FakeSource(open_rows=[_open("A"), _open("B", price=0.31, prev=0.44)])
    _patch_sources(monkeypatch, healthy)
    assert run(aud, slot="am", now=NOW + timedelta(hours=13), force=True) is not None
    b = Store(tmp_path / "data").load_state()["breakers"]["k"]
    assert b == {"failures": 0, "disabled": False, "disabled_at": None}


# ------------------------------------------------------- run: watch backfill
def test_watch_markets_are_fetched_by_id_and_always_snapshotted(tmp_path, monkeypatch):
    aud = _aud(tmp_path)
    thin = _open("SEN-D", price=0.52, prev=0.5, vol=115)      # below snapshot_min_volume_24h
    src = FakeSource(open_rows=[_open("A")], by_id={"SEN-D": thin})
    _patch_sources(monkeypatch, src)
    run(aud, slot="am", now=NOW)
    assert src.id_calls == [["SEN-D"]]
    store = Store(tmp_path / "data")
    prices = store.price_map_at_or_before("k", NOW)[1]
    assert "SEN-D" in prices and "A" in prices


def test_a_watch_backfill_failure_does_not_trip_the_breaker(tmp_path, monkeypatch):
    aud = _aud(tmp_path)
    src = FakeSource(open_rows=[_open("A"), _open("B", price=0.31, prev=0.44)])
    src.fetch_by_ids = lambda ids: (_ for _ in ()).throw(RuntimeError("404"))
    _patch_sources(monkeypatch, src)
    assert run(aud, slot="am", now=NOW) is not None
    assert Store(tmp_path / "data").load_state()["breakers"]["k"]["failures"] == 0


# ------------------------------------------------- run: settled eligibility
def test_history_budget_and_ledger_skip_short_lived_markets(tmp_path, monkeypatch):
    aud = _aud(tmp_path)
    src = FakeSource(settled=[_settled("QUARTER", lifetime_h=0.25), _settled("REAL", lifetime_h=240)])
    _patch_sources(monkeypatch, src)
    run(aud, slot="pm", now=NOW)
    assert src.history_calls == ["REAL"]
    rows = Store(tmp_path / "data").load_resolutions()
    assert [r["ticker"] for r in rows] == ["REAL"]
    assert rows[0]["price_24h_before"] == "0.4"


def test_excluded_titles_never_reach_the_ledger(tmp_path, monkeypatch):
    aud = _aud(tmp_path, exclude_title_regex="price up in next|next \\d+ mins")
    src = FakeSource(settled=[_settled("BTC15", title="BTC price up in next 15 mins?"),
                              _settled("REAL")])
    _patch_sources(monkeypatch, src)
    run(aud, slot="pm", now=NOW)
    assert src.history_calls == ["REAL"]
    assert [r["ticker"] for r in Store(tmp_path / "data").load_resolutions()] == ["REAL"]


def test_dry_run_writes_no_chart_and_no_state(tmp_path, monkeypatch):
    aud = _aud(tmp_path)
    _patch_sources(monkeypatch, FakeSource(open_rows=[_open("A"), _open("B", price=0.31, prev=0.44)]))
    post = run(aud, slot="am", now=NOW, dry_run=True)
    assert post is not None and post.chart == "/charts/2026-08-17-am.png"
    assert not (tmp_path / "site").exists()
    assert not (tmp_path / "data" / "state.json").exists()


# ---------------------------------------------------------------- state store
def test_snapshot_write_is_atomic_and_cached(tmp_path):
    store = Store(tmp_path / "data")
    path = store.save_snapshot("k", [_open("A", price=0.2)], NOW)
    assert list(path.parent.glob("*.tmp")) == []
    first = store.snapshot_prices(path)
    assert first == {"A": 0.2} and store.snapshot_prices(path) is first


def test_a_damaged_snapshot_reads_as_empty_instead_of_raising(tmp_path):
    store = Store(tmp_path / "data")
    path = store.save_snapshot("k", [_open("A")], NOW)
    path.write_bytes(gzip.compress(b"ticker,yes_price\n")[:12])   # truncated gzip
    store._drop_caches()
    assert store.snapshot_prices(path) == {}


# ------------------------------------------------------------------ sources
class FakeHttp:
    def __init__(self, answers):
        self.answers = answers          # (url_tail) -> list of responses
        self.calls: list[tuple[str, dict]] = []

    def get_json(self, url, params=None, tries=5, min_sleep=0.0):
        self.calls.append((url, dict(params or {})))
        for tail, queue in self.answers.items():
            if url.endswith(tail):
                return queue.pop(0) if len(queue) > 1 else queue[0]
        raise AssertionError(f"unexpected GET {url}")


def _kalshi_market(ticker, close_time, result="yes"):
    return {"ticker": ticker, "event_ticker": "EV-1", "title": f"Will {ticker}?",
            "last_price_dollars": "0.5", "volume_24h_fp": "1000", "close_time": close_time,
            "open_time": "2026-08-01T00:00:00Z", "result": result, "settlement_ts": close_time}


def test_kalshi_settled_filters_server_side_and_never_stops_early():
    fresh = "2026-08-17T12:00:00Z"
    old = "2026-08-10T00:00:00Z"
    http = FakeHttp({
        "/series": [{"series": [{"ticker": "EV", "category": "Politics"}]}],
        "/markets": [
            # page 1 ends on an old market: the walk must continue anyway
            {"cursor": "c2", "markets": [_kalshi_market("A", fresh), _kalshi_market("OLD", old)]},
            {"cursor": "", "markets": [_kalshi_market("B", fresh)]},
        ],
    })
    since = datetime(2026, 8, 16, 11, 0, tzinfo=timezone.utc)
    rows = KalshiSource({}, http).fetch_settled(since)
    assert [r.ticker for r in rows] == ["A", "B"]
    market_calls = [c for c in http.calls if c[0].endswith("/markets")]
    assert market_calls[0][1]["min_close_ts"] == int(since.timestamp())
    assert rows[0].open_time == "2026-08-01T00:00:00Z"


def test_kalshi_fetch_by_ids_asks_in_chunks_of_100():
    ids = [f"T{i}" for i in range(150)]
    http = FakeHttp({"/series": [{"series": []}],
                     "/markets": [{"markets": [_kalshi_market("T0", "2026-09-01T00:00:00Z")]}]})
    src = KalshiSource({"page_sleep": 0}, http)
    src.fetch_by_ids(ids)
    chunks = [c[1]["tickers"].split(",") for c in http.calls if "tickers" in c[1]]
    assert [len(c) for c in chunks] == [100, 50]


def test_polymarket_settled_query_carries_the_volume_floor():
    http = FakeHttp({"/markets": [[]]})
    PolymarketSource({"settled_volume_min": 10000, "page_sleep": 0}, http) \
        .fetch_settled(datetime(2026, 8, 16, 11, 0, tzinfo=timezone.utc))
    assert http.calls[0][1]["volume_num_min"] == 10000


def test_polymarket_fetch_by_ids_asks_one_slug_at_a_time():
    market = {"slug": "senate-2026", "question": "Will the Democrats win?",
              "outcomes": '["Yes","No"]', "outcomePrices": '["0.52","0.48"]',
              "volume24hr": 115, "endDate": "2026-11-03T00:00:00Z",
              "startDate": "2025-07-11T19:48:59Z", "events": [{"slug": "e1"}]}
    http = FakeHttp({"/markets": [[market]]})
    rows = PolymarketSource({"page_sleep": 0}, http).fetch_by_ids(["senate-2026"])
    assert [c[1]["slug"] for c in http.calls] == ["senate-2026"]
    assert rows[0].ticker == "senate-2026" and rows[0].yes_price == 0.52
    assert rows[0].open_time == "2025-07-11T19:48:59Z"


# --------------------------------------------------------------- post text
def test_delta_pts_agrees_with_the_printed_prices():
    assert delta_pts(0.045, 0.925) == "+88"          # 5¢ -> 92¢ prints +87? no: 92-5
    assert delta_pts(0.05, 0.925) == "+87"
    assert delta_pts(0.44, 0.31) == "-13"
    assert delta_pts(None, 0.5) == "—"


def test_fit_lines_shrinks_titles_before_it_drops_a_line():
    def make(clip_n):
        return [f"K · {('T' * 80)[:clip_n]} 12¢→41¢ (+29) · $20k" for _ in range(4)]
    full = "\n".join(["Biggest odds swings, last 24h", *make(48), "#Tag"])
    assert len(full) > 280                       # all four lines do not fit at clip 48
    text = fit_lines("Biggest odds swings, last 24h", make, "#Tag", clips=(48, 36, 28))
    assert len(text) <= 280
    assert len(text.splitlines()) == 6           # header + 4 lines + footer, none dropped
    assert "T" * 28 in text and "T" * 48 not in text


def test_fit_lines_keeps_the_longest_title_that_still_fits():
    def make(clip_n):
        return [f"UPSET · K · {('T' * 80)[:clip_n]} → NO · was 3¢ ~24h before · $157k"
                for _ in range(4)]
    # clip 44 fits 3 lines; both 34 and 26 fit all 4, so 34 wins
    text = fit_lines("Settled in the last 24h", make, "#Tag", clips=(44, 34, 26))
    assert len([ln for ln in text.splitlines() if "T" in ln]) == 4
    assert "T" * 34 in text and "T" * 35 not in text


def test_fit_text_still_drops_lines_when_nothing_else_fits():
    assert len(fit_text("H", ["a" * 100] * 4, "#t")) <= 280


def _ctx(tmp_path, slot, open_rows=(), settled_rows=(), state=None, cfg=None):
    return Context(now=NOW, audience=_aud(tmp_path), store=Store(tmp_path / "data"),
                   open_rows=list(open_rows), settled_rows=list(settled_rows), labels={"k": "K", "p": "P"},
                   slot=slot, state=state if state is not None else {},
                   extra={"shape_cfg": cfg or {"slot": slot}})


def test_movers_shows_one_market_per_question_across_venues(tmp_path):
    same_on_p = _open("A2", price=0.42, prev=0.13)
    same_on_p.venue = "p"
    same_on_p.title = "Will A happen?"           # the same question, the other venue
    rows = [_open("A"), same_on_p, _open("B", price=0.31, prev=0.44)]
    for r in rows:
        r.change_24h = r.yes_price - r.prev_24h_price
    post = build_shape({"slot": "am", "type": "movers"}).build(
        _ctx(tmp_path, "am", open_rows=rows, cfg={"slot": "am", "type": "movers"}))
    assert [r["ticker"] for r in post.rows] == ["A", "B"]


def test_settled_does_not_feature_the_same_market_two_days_running(tmp_path):
    row = _settled("BIG")
    row.p24 = 0.1
    state = {}
    cfg = {"slot": "pm", "type": "settled"}
    first = build_shape(cfg).build(_ctx(tmp_path, "pm", settled_rows=[row], state=state, cfg=cfg))
    assert first is not None and state["settled_featured"] == {"k:BIG": "2026-08-17"}

    later = _ctx(tmp_path, "pm", settled_rows=[row], state=state, cfg=cfg)
    later.now = NOW + timedelta(days=1)
    assert build_shape(cfg).build(later) is None


# ---------------------------------------------------------------- healthcheck
def _healthcheck_problems(tmp_path, monkeypatch, posts, state):
    """Run healthcheck.main() against tmp_path with every network call faked."""
    aud = _aud(tmp_path)
    aud.raw["healthcheck"] = {"max_post_age_hours": 30}
    store = Store(tmp_path / "data")
    store.posts_path.write_text(json.dumps(posts))
    store.save_state(state)
    ok = type("R", (), {"status_code": 200, "text": "<feed", "json": lambda self: {}})()
    monkeypatch.setattr(hc_mod, "load_audience", lambda name: aud)
    monkeypatch.setattr(hc_mod, "_get", lambda url, **kw: ok)
    monkeypatch.setattr(hc_mod, "bsky_profile", lambda h: {"followers": 1, "posts": 1, "ok": True})
    monkeypatch.setattr(hc_mod, "gh_repo", lambda r: {"stars": 0, "watchers": 0, "forks": 0, "size_kb": 0})
    monkeypatch.setattr(hc_mod, "_issue_upsert", lambda *a: None)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    hc_mod.main(["--audience", "t"])
    return store.load_state()["healthcheck"]["last"]["problems"]


def test_healthcheck_reports_a_stale_newest_post(tmp_path, monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(hours=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh_run = {"slots_done": {}, "breakers": {},
                 "last_run": {"at": datetime.now(timezone.utc).isoformat(),
                              "no_post_reason": "nothing qualified"}}
    problems = _healthcheck_problems(tmp_path, monkeypatch, [{"id": "x", "published_at": old}], fresh_run)
    assert "newest post 40h old" in problems and "nothing qualified" in problems


def test_healthcheck_reports_three_slots_in_a_row_with_no_post(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    state = {"breakers": {}, "last_run": {"at": now.isoformat()},
             "slots_done": {"2026-08-15-am": "t (no post)", "2026-08-16-am": "t (skipped)",
                            "2026-08-17-am": "t (no post)"}}
    problems = _healthcheck_problems(tmp_path, monkeypatch,
                                     [{"id": "x", "published_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}],
                                     state)
    assert "last 3 slots produced no post" in problems


def test_healthcheck_is_quiet_when_the_feed_is_fresh(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    state = {"breakers": {}, "last_run": {"at": now.isoformat()},
             "slots_done": {"2026-08-17-am": "t"}}
    problems = _healthcheck_problems(tmp_path, monkeypatch,
                                     [{"id": "x", "published_at": now.strftime("%Y-%m-%dT%H:%M:%SZ")}],
                                     state)
    assert problems == ""
