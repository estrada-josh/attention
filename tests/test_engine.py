"""Zero-network tests for the engine's pure parts."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from engine.config import Audience
from engine.model import MarketRow
from engine.render import text_to_html, table_html
from engine.shapes import Context, build_shape
from engine.shapes.base import fit_text, cents, pts
from engine.state import Store


def _aud(tmp_path: Path) -> Audience:
    return Audience(name="t", display_name="T (bot)", domain="t.example.com", site_url="https://t.example.com",
                    description="d", contact_email="t@example.com", repo="x/y", site_root="audiences/t/site",
                    tags=["PredictionMarkets"], sources=[], shapes=[], channels=[],
                    thresholds={"exclude_categories": ["Sports"], "min_volume_24h": {"k": 5000}, "movers_min_points": 8,
                                "venue_tags": {"k": "Kalshi"}, "settled_min_volume": 10000},
                    raw={})


def _row(venue, ticker, price, prev, vol=20000, cat="Politics", close_h=48, **kw):
    now = datetime(2026, 8, 16, 13, 17, tzinfo=timezone.utc)
    return MarketRow(venue=venue, ticker=ticker, title=f"Will {ticker} happen?", yes_price=price, prev_24h_price=prev,
                     change_24h=price - prev, volume_24h=vol, category=cat,
                     close_time=(now + timedelta(hours=close_h)).strftime("%Y-%m-%dT%H:%M:%SZ"), url="https://x/y", **kw)


def test_fit_text_drops_lines_to_limit():
    t = fit_text("H", ["a" * 100, "b" * 100, "c" * 100], "#tag", limit=280)
    assert len(t) <= 280 and t.startswith("H") and t.endswith("#tag") and "c" * 100 not in t


def test_cents_pts():
    assert cents(0.415) == "42¢" and pts(0.29) == "+29" and pts(-0.13) == "-13"


def test_movers_selects_and_ranks(tmp_path):
    aud = _aud(tmp_path)
    store = Store(tmp_path / "data")
    now = datetime(2026, 8, 16, 13, 17, tzinfo=timezone.utc)
    rows = [_row("k", "A", 0.41, 0.12), _row("k", "B", 0.31, 0.44), _row("k", "SPORT", 0.9, 0.1, cat="Sports"),
            _row("k", "SMALL", 0.5, 0.45), _row("k", "LOWVOL", 0.9, 0.1, vol=100), _row("k", "SOON", 0.9, 0.1, close_h=2)]
    ctx = Context(now=now, audience=aud, store=store, open_rows=rows, settled_rows=[], labels={"k": "K"}, slot="am",
                  state={}, extra={"shape_cfg": {"slot": "am", "type": "movers"}})
    post = build_shape({"slot": "am", "type": "movers"}).build(ctx)
    assert post is not None
    assert [r["ticker"] for r in post.rows] == ["A", "B"]
    assert len(post.text) <= 280 and "12¢→41¢ (+29)" in post.text and "#Kalshi" in post.text
    assert post.id == "2026-08-16-am"


def test_movers_returns_none_when_too_few(tmp_path):
    aud = _aud(tmp_path)
    now = datetime(2026, 8, 16, 13, 17, tzinfo=timezone.utc)
    ctx = Context(now=now, audience=aud, store=Store(tmp_path / "d"), open_rows=[_row("k", "A", 0.41, 0.12)],
                  settled_rows=[], labels={"k": "K"}, slot="am", state={}, extra={"shape_cfg": {"slot": "am", "type": "movers"}})
    assert build_shape({"slot": "am", "type": "movers"}).build(ctx) is None


def test_settled_upsets_first(tmp_path):
    aud = _aud(tmp_path)
    now = datetime(2026, 8, 16, 22, 17, tzinfo=timezone.utc)
    a = _row("k", "UP", 1.0, 0.1, status="settled", result="yes"); a.p24 = 0.10
    b = _row("k", "BIG", 0.0, 0.5, vol=900000, status="settled", result="no"); b.p24 = 0.50
    ctx = Context(now=now, audience=aud, store=Store(tmp_path / "d"), open_rows=[], settled_rows=[a, b],
                  labels={"k": "K"}, slot="pm", state={}, extra={"shape_cfg": {"slot": "pm", "type": "settled"}})
    post = build_shape({"slot": "pm", "type": "settled"}).build(ctx)
    assert post is not None and post.rows[0]["ticker"] == "UP" and post.rows[0]["upset"] is True
    assert post.text.splitlines()[1].startswith("UPSET")


def test_store_snapshot_roundtrip_and_prev_lookup(tmp_path):
    store = Store(tmp_path / "data")
    now = datetime(2026, 8, 16, 13, 0, tzinfo=timezone.utc)
    store.save_snapshot("k", [_row("k", "A", 0.2, 0.2)], now - timedelta(hours=24))
    got = store.snapshot_at_or_before("k", now - timedelta(hours=22), max_age=timedelta(hours=14))
    assert got is not None and got[1][0].ticker == "A" and got[1][0].yes_price == 0.2 and got[1][0].venue == "k"
    assert store.snapshot_at_or_before("k", now - timedelta(days=5)) is None


def test_state_slots_and_resolutions(tmp_path):
    store = Store(tmp_path / "data")
    st = store.load_state(); st["slots_done"]["x"] = "t"; store.save_state(st)
    assert store.load_state()["slots_done"]["x"] == "t"
    n = store.append_resolutions([{"venue": "k", "ticker": "A", "result": "yes", "settled_at": "2026-08-16T00:00:00Z"}])
    n2 = store.append_resolutions([{"venue": "k", "ticker": "A", "result": "yes", "settled_at": "2026-08-16T00:00:00Z"}])
    assert (n, n2) == (1, 0)


def test_text_to_html_links_tags_and_domain():
    h = text_to_html("Hi #Tag\nt.example.com/calibration", "https://t.example.com")
    assert '<a href="/tag/tag">#Tag</a>' in h and 'href="https://t.example.com/calibration"' in h and "<br>" in h


def test_table_html_movers():
    h = table_html({"shape": "movers", "rows": [{"label": "K", "title": "T", "subtitle": "", "prev": 0.1, "now": 0.2,
                                                  "delta": 0.1, "volume_24h": 1000, "close_time": "2026-09-01T00:00:00Z", "url": "https://x"}]})
    assert "<table>" in h and "10¢" in h and "+10" in h
