"""One pipeline run for one audience and one slot.

Steps: resolve slot -> fetch sources (with breakers) -> enrich 24h change
from our own snapshots -> save snapshots + resolutions -> build the slot's
post -> chart -> post index -> render site. Publishing to channels is a
separate step (engine.publish) because it must wait for the site to be live.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .charts import render_chart
from .config import Audience
from .http import Http
from .model import MarketRow, Post
from .shapes import Context, build_shape
from .sources import build_sources
from .state import Store

log = logging.getLogger("engine.run")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def resolve_slot(audience: Audience, now: datetime, wanted: str) -> dict | None:
    """Pick the shape config for `wanted`, or for 'auto' the one whose `at`
    (HH:MM UTC) is nearest and within 90 minutes before now, honoring `days`
    (0=Mon..6=Sun) and `until` (YYYY-MM-DD)."""
    def active(s: dict) -> bool:
        if s.get("days") is not None and now.weekday() not in s["days"]:
            return False
        if s.get("until") and now.strftime("%Y-%m-%d") > s["until"]:
            return False
        if s.get("from") and now.strftime("%Y-%m-%d") < s["from"]:
            return False
        return True

    if wanted != "auto":
        s = audience.shape(wanted)
        return s if s and active(s) else (s if s and wanted else None)
    best = None
    for s in audience.shapes:
        if not s.get("at") or not active(s):
            continue
        hh, mm = (int(x) for x in s["at"].split(":"))
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        delta = (now - at).total_seconds()
        if 0 <= delta <= 90 * 60 and (best is None or delta < best[0]):
            best = (delta, s)
    return best[1] if best else None


def enrich_24h(rows: list[MarketRow], store: Store, venue: str, now: datetime) -> None:
    """Fill prev_24h_price / change_24h from our snapshot nearest to now-24h.
    Falls back to the venue-provided value when we have no snapshot."""
    snap = store.snapshot_at_or_before(venue, now - timedelta(hours=22), max_age=timedelta(hours=14))
    prev_map: dict[str, float] = {}
    if snap:
        ts, old = snap
        prev_map = {r.ticker: r.yes_price for r in old if r.yes_price is not None}
        log.info("%s: 24h reference snapshot %s (%d rows)", venue, ts.isoformat(), len(prev_map))
    for r in rows:
        if r.yes_price is None:
            continue
        p = prev_map.get(r.ticker)
        if p is None:
            # venue-provided fallback (Polymarket oneDayPriceChange, Kalshi previous_price)
            if r.prev_24h_price is not None:
                p = r.prev_24h_price
            else:
                vp = getattr(r, "extra_prev", None)
                ot = getattr(r, "extra_open_time", None)
                if vp not in (None, 0.0) and ot and ot < (now - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ"):
                    p = vp
        if p is None:
            r.prev_24h_price = None
            r.change_24h = None
        else:
            r.prev_24h_price = p
            r.change_24h = r.yes_price - p


def price_before(store: Store, venue: str, ticker: str, when: datetime, hours: int) -> float | None:
    snap = store.snapshot_at_or_before(venue, when - timedelta(hours=hours), max_age=timedelta(hours=hours + 14))
    if not snap:
        return None
    for r in snap[1]:
        if r.ticker == ticker:
            return r.yes_price
    return None


def _settle_time(r: MarketRow) -> datetime | None:
    raw = r.settled_at or r.close_time
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def keep_for_snapshot(r: MarketRow, min_vol: float, min_oi: float, excl: set[str] = frozenset()) -> bool:
    if r.category in excl:
        return False
    return (r.volume_24h or 0) >= min_vol or (r.open_interest or 0) >= min_oi


def run(audience: Audience, slot: str = "auto", now: datetime | None = None,
        dry_run: bool = False, force: bool = False) -> Post | None:
    now = now or utcnow()
    store = Store(audience.data_dir)
    state = store.load_state()
    shape_cfg = resolve_slot(audience, now, slot)
    if shape_cfg is None:
        log.info("no slot due at %s (wanted=%s); fetching for snapshots only", now.isoformat(), slot)
    slot_name = shape_cfg["slot"] if shape_cfg else "snapshot"
    slot_id = f"{now.strftime('%Y-%m-%d')}-{slot_name}"
    if shape_cfg and slot_id in state.get("slots_done", {}) and not force:
        log.info("slot %s already done at %s; nothing to do", slot_id, state["slots_done"][slot_id])
        return None

    ua = f"{audience.display_name}/0.1 (+{audience.site_url}; {audience.contact_email})"
    http = Http(ua)
    sources = build_sources(audience.sources, http)
    labels = {s.name: s.label for s in sources}
    min_vol = float(audience.thresholds.get("snapshot_min_volume_24h", 1))
    min_oi = float(audience.thresholds.get("snapshot_min_open_interest", 100))
    store_excl = set(audience.thresholds.get("store_exclude_categories", []) or [])

    open_rows: list[MarketRow] = []
    settled_rows: list[MarketRow] = []
    breakers = state.setdefault("breakers", {})
    for src in sources:
        b = breakers.setdefault(src.name, {"failures": 0, "disabled": False})
        if b.get("disabled"):
            log.warning("%s: source disabled by breaker (%d failures)", src.name, b["failures"])
            continue
        try:
            rows = src.fetch_open()
            enrich_24h(rows, store, src.name, now)
            keep = [r for r in rows if keep_for_snapshot(r, min_vol, min_oi, store_excl)]
            if not dry_run:
                store.save_snapshot(src.name, keep, now)
            open_rows.extend(rows)
            settled = src.fetch_settled(now - timedelta(hours=26))
            excl = set(audience.thresholds.get("exclude_categories", []) or [])
            hist_budget = int(audience.thresholds.get("history_lookups_per_source", 40))
            settled.sort(key=lambda r: r.volume_24h or 0, reverse=True)
            for r in settled:
                when = _settle_time(r) or now
                r.p24 = price_before(store, src.name, r.ticker, when, 24)  # type: ignore[attr-defined]
                if r.p24 is None and hist_budget > 0 and r.category not in excl \
                        and (r.volume_24h or 0) >= float(audience.thresholds.get("settled_min_volume", 10000)):
                    r.p24 = src.history_price(r, when - timedelta(hours=24))  # type: ignore[attr-defined]
                    hist_budget -= 1
            settled_rows.extend(settled)
            b["failures"] = 0
        except Exception as e:  # noqa: BLE001
            b["failures"] = b.get("failures", 0) + 1
            log.error("%s: fetch failed (%d consecutive): %s", src.name, b["failures"], e)
            if b["failures"] >= int(audience.thresholds.get("breaker_failures", 3)):
                b["disabled"] = True
                log.error("%s: breaker OPEN — source disabled until state.json is edited", src.name)

    # resolutions ledger (only rows with a known 24h-before price are useful for calibration,
    # but we record all with the price fields we have)
    res_rows = []
    res_min_vol = float(audience.thresholds.get("resolution_min_volume_24h", 100))
    for r in settled_rows:
        if r.category in store_excl or (r.volume_24h or 0) < res_min_vol:
            continue
        res_rows.append({
            "venue": r.venue, "ticker": r.ticker, "title": r.title, "subtitle": r.subtitle,
            "result": r.result, "settled_at": r.settled_at or "", "close_time": r.close_time or "",
            "last_price_before": r.yes_price if r.yes_price is not None else "",
            "price_24h_before": getattr(r, "p24", None) if getattr(r, "p24", None) is not None else "",
            "price_7d_before": "", "volume_24h": r.volume_24h or 0, "url": r.url,
            "recorded_at": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        })
    if not dry_run and res_rows:
        n = store.append_resolutions(res_rows)
        log.info("resolutions: +%d rows", n)

    post = None
    if shape_cfg and open_rows or shape_cfg and settled_rows:
        shape = build_shape(shape_cfg)
        ctx = Context(now=now, audience=audience, store=store, open_rows=open_rows,
                      settled_rows=settled_rows, labels=labels, slot=shape_cfg["slot"], state=state,
                      extra={"shape_cfg": shape_cfg})
        post = shape.build(ctx)
        if post is None:
            log.info("shape %s produced no post (nothing qualified)", shape.name)
        else:
            kind = post.extra.get("chart_kind")
            if kind:
                chart_rel = f"charts/{post.id}.png"
                path = render_chart(kind, post.rows, post.title.split(" — ")[0],
                                    f"{now.strftime('%b %-d, %Y')} · " + " + ".join(s.name.title() for s in sources),
                                    audience.domain, audience.site_dir / chart_rel)
                if path:
                    post.chart = "/" + chart_rel
                    post.chart_alt = post.text
            if not dry_run:
                store.add_post(post)
    if not dry_run:
        if shape_cfg:
            state.setdefault("slots_done", {})[slot_id] = now.isoformat()
            if post is None:
                state["slots_done"][slot_id] += " (no post)"
        state["last_run"] = {"at": now.isoformat(), "slot": slot_name, "open_rows": len(open_rows),
                             "settled_rows": len(settled_rows), "post": post.id if post else None}
        store.save_state(state)
        store.prune_snapshots(int(audience.thresholds.get("snapshot_keep_days", 45)))
    return post
