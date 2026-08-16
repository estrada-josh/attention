"""One pipeline run for one audience and one slot.

Steps: resolve slot -> fetch sources (with breakers) -> enrich 24h change
from our own snapshots -> save snapshots + resolutions -> build the slot's
post -> chart -> post index -> render site. Publishing to channels is a
separate step (engine.publish) because it must wait for the site to be live.
"""
from __future__ import annotations

import logging
import re
import tempfile
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


def parse_iso(raw: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp from any venue, or return None."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def resolve_slot(audience: Audience, now: datetime, wanted: str, state: dict | None = None) -> dict | None:
    """Pick the shape config for `wanted`, or for 'auto' the earliest shape that
    is due today, not already done, and less than `slot_grace_minutes` late.

    Earliest first: with two cron ticks per slot, a later tick can then still
    post a slot that both of its own ticks missed. A shape that is past the
    grace window is written to slots_done as "(skipped)" so the gap is visible.
    """
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
        if s is None:
            raise SystemExit(f"unknown slot {wanted!r}; known: {[x.get('slot') for x in audience.shapes]}")
        return s

    state = state if state is not None else {}
    done = state.get("slots_done") or {}
    grace = float(audience.thresholds.get("slot_grace_minutes", 240))
    due: list[tuple[float, dict]] = []
    for s in audience.shapes:
        if not s.get("at") or not active(s):
            continue
        hh, mm = (int(x) for x in s["at"].split(":"))
        at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        late = (now - at).total_seconds() / 60.0
        slot_id = f"{now.strftime('%Y-%m-%d')}-{s['slot']}"
        if late < 0 or slot_id in done:
            continue
        if late <= grace:
            due.append((late, s))
        else:
            log.warning("::warning::slot %s skipped: %.0f min late, grace is %.0f min",
                        slot_id, late, grace)
            state.setdefault("slots_done", {})[slot_id] = now.isoformat() + " (skipped)"
    if not due:
        return None
    due.sort(key=lambda t: t[0], reverse=True)   # most late = earliest `at`
    return due[0][1]


def watch_ids(audience: Audience) -> dict[str, list[str]]:
    """venue -> the market ids that any watchlist shape names.

    A paged list scan can miss a thin or long-dated market, so the engine also
    fetches these by id and always keeps them in the snapshot.
    """
    out: dict[str, list[str]] = {}
    for s in audience.shapes:
        for w in s.get("watch") or []:
            for m in w.get("match") or []:
                venue, ticker = m.get("venue"), m.get("ticker")
                if venue and ticker and ticker not in out.setdefault(venue, []):
                    out[venue].append(ticker)
    return out


def enrich_24h(rows: list[MarketRow], store: Store, venue: str, now: datetime) -> None:
    """Fill prev_24h_price / change_24h from our snapshot nearest to now-24h.
    Falls back to the venue-provided value when we have no snapshot."""
    snap = store.price_map_at_or_before(venue, now - timedelta(hours=22), max_age=timedelta(hours=14))
    prev_map: dict[str, float] = {}
    if snap:
        ts, prev_map = snap
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
                ot = parse_iso(r.open_time)
                if vp not in (None, 0.0) and ot and ot < now - timedelta(hours=24):
                    p = vp
        if p is None:
            r.prev_24h_price = None
            r.change_24h = None
        else:
            r.prev_24h_price = p
            r.change_24h = r.yes_price - p


def price_before(store: Store, venue: str, ticker: str, when: datetime, hours: int) -> float | None:
    """Our own price for `ticker` from the snapshot nearest before `when - hours`.
    One gunzip per distinct snapshot file per run; every later call is a dict hit."""
    snap = store.price_map_at_or_before(venue, when - timedelta(hours=hours),
                                        max_age=timedelta(hours=hours + 14))
    if not snap:
        return None
    return snap[1].get(ticker)


def _settle_time(r: MarketRow) -> datetime | None:
    return parse_iso(r.settled_at or r.close_time)


def keep_for_snapshot(r: MarketRow, min_vol: float, min_oi: float, excl: set[str] = frozenset()) -> bool:
    if r.category in excl:
        return False
    return (r.volume_24h or 0) >= min_vol or (r.open_interest or 0) >= min_oi


def run(audience: Audience, slot: str = "auto", now: datetime | None = None,
        dry_run: bool = False, force: bool = False) -> Post | None:
    now = now or utcnow()
    store = Store(audience.data_dir)
    state = store.load_state()
    shape_cfg = resolve_slot(audience, now, slot, state)
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
    th = audience.thresholds
    min_vol = float(th.get("snapshot_min_volume_24h", 1))
    min_oi = float(th.get("snapshot_min_open_interest", 100))
    store_excl = set(th.get("store_exclude_categories", []) or [])
    excl = set(th.get("exclude_categories", []) or [])
    title_rx = re.compile(th.get("exclude_title_regex", "$^"), re.I)
    min_lifetime = timedelta(hours=float(th.get("settled_min_lifetime_hours", 30)))
    settled_min_vol = float(th.get("settled_min_volume", 10000))
    watch = watch_ids(audience)

    def usable(r: MarketRow, when: datetime) -> bool:
        """True when a settled market can carry a 24h-before price worth keeping.

        A market that opened 15 minutes before it settled has no price 24h
        earlier, so a history lookup for it burns budget and a ledger row for it
        can never be scored.
        """
        if r.category in excl or title_rx.search(r.title):
            return False
        opened = parse_iso(r.open_time)
        return opened is None or (when - opened) >= min_lifetime

    open_rows: list[MarketRow] = []
    settled_rows: list[MarketRow] = []
    used_sources: list = []
    failed: list[str] = []
    breakers = state.setdefault("breakers", {})
    cooldown = timedelta(hours=float(th.get("breaker_cooldown_hours", 12)))
    for src in sources:
        b = breakers.setdefault(src.name, {"failures": 0, "disabled": False})
        if b.get("disabled"):
            since = parse_iso(b.get("disabled_at"))
            if since is None or now - since >= cooldown:
                log.warning("%s: breaker half-open after the %s cooldown; trying the source once",
                            src.name, cooldown)
            else:
                log.warning("%s: source disabled by breaker (%d failures, since %s)",
                            src.name, b["failures"], b.get("disabled_at", "?"))
                continue
        try:
            pinned = set(watch.get(src.name, []))
            rows = src.fetch_open()
            missing = sorted(pinned - {r.ticker for r in rows})
            if missing:
                try:
                    rows.extend(src.fetch_by_ids(missing))
                except Exception as e:  # noqa: BLE001  (a watch miss must not trip the breaker)
                    log.warning("%s: watch backfill failed for %s: %s", src.name, missing, e)
                gone = pinned - {r.ticker for r in rows}
                if gone:
                    log.warning("%s: watch markets not found: %s", src.name, sorted(gone))
            enrich_24h(rows, store, src.name, now)
            keep = [r for r in rows
                    if r.ticker in pinned or keep_for_snapshot(r, min_vol, min_oi, store_excl)]
            if not dry_run:
                store.save_snapshot(src.name, keep, now)
            open_rows.extend(rows)

            settled = src.fetch_settled(now - timedelta(hours=26))
            hist_budget = int(th.get("history_lookups_per_source", 40))
            settled.sort(key=lambda r: r.volume_24h or 0, reverse=True)
            not_scorable = 0
            for r in settled:
                when = _settle_time(r) or now
                r.usable = usable(r, when)                                   # type: ignore[attr-defined]
                if not r.usable:
                    not_scorable += 1
                # a row in an excluded category is never in a snapshot
                r.p24 = None if r.category in store_excl else \
                    price_before(store, src.name, r.ticker, when, 24)        # type: ignore[attr-defined]
                if r.p24 is None and r.usable and hist_budget > 0 \
                        and (r.volume_24h or 0) >= settled_min_vol:
                    r.p24 = src.history_price(r, when - timedelta(hours=24))  # type: ignore[attr-defined]
                    hist_budget -= 1
            log.info("%s: %d settled rows, %d not scorable (category, title, or too short-lived), "
                     "%d history lookups left", src.name, len(settled), not_scorable, hist_budget)
            settled_rows.extend(settled)
            b.update({"failures": 0, "disabled": False, "disabled_at": None})
            used_sources.append(src)
        except Exception as e:  # noqa: BLE001
            failed.append(src.name)
            b["failures"] = b.get("failures", 0) + 1
            log.error("%s: fetch failed (%d consecutive): %s", src.name, b["failures"], e)
            if b["failures"] >= int(th.get("breaker_failures", 3)):
                b["disabled"] = True
                b["disabled_at"] = now.isoformat()
                log.error("%s: breaker OPEN — source paused for %s (or until "
                          "`python -m engine reset-breaker`)", src.name, cooldown)

    # resolutions ledger (only rows with a known 24h-before price are useful for calibration,
    # but we record all with the price fields we have)
    res_rows = []
    res_min_vol = float(th.get("resolution_min_volume_24h", 100))
    for r in settled_rows:
        if r.category in store_excl or (r.volume_24h or 0) < res_min_vol:
            continue
        if not getattr(r, "usable", True):
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
    no_post_reason = None if shape_cfg else "no slot due"
    if shape_cfg and not (open_rows or settled_rows):
        no_post_reason = f"sources failed: {','.join(failed)}" if failed else "no rows"
    if shape_cfg and (open_rows or settled_rows):
        shape = build_shape(shape_cfg)
        ctx = Context(now=now, audience=audience, store=store, open_rows=open_rows,
                      settled_rows=settled_rows, labels=labels, slot=shape_cfg["slot"], state=state,
                      extra={"shape_cfg": shape_cfg})
        post = shape.build(ctx)
        if post is None:
            no_post_reason = "nothing qualified"
            log.info("shape %s produced no post (nothing qualified)", shape.name)
        else:
            kind = post.extra.get("chart_kind")
            if kind:
                chart_rel = f"charts/{post.id}.png"
                subtitle = f"{now.strftime('%b %-d, %Y')} · " + \
                           " + ".join(s.name.title() for s in used_sources)
                title = post.title.split(" — ")[0]
                if dry_run:
                    # a dry run must not overwrite the published chart in site/
                    with tempfile.TemporaryDirectory() as td:
                        path = render_chart(kind, post.rows, title, subtitle,
                                            audience.domain, Path(td) / chart_rel)
                else:
                    path = render_chart(kind, post.rows, title, subtitle,
                                        audience.domain, audience.site_dir / chart_rel)
                if path:
                    post.chart = "/" + chart_rel
                    post.chart_alt = post.text
            if not dry_run:
                store.add_post(post)
    if not dry_run:
        if shape_cfg and post is not None:
            note = f" (missing: {','.join(sorted(failed))})" if failed else ""
            state.setdefault("slots_done", {})[slot_id] = now.isoformat() + note
        elif shape_cfg and failed:
            # leave the slot open so the next tick retries it while the window lasts
            log.error("slot %s left open: sources failed (%s); the next tick retries it",
                      slot_id, ",".join(failed))
        elif shape_cfg:
            state.setdefault("slots_done", {})[slot_id] = now.isoformat() + " (no post)"
        state["last_run"] = {"at": now.isoformat(), "slot": slot_name, "open_rows": len(open_rows),
                             "settled_rows": len(settled_rows), "post": post.id if post else None,
                             "no_post_reason": no_post_reason, "failed_sources": failed}
        store.save_state(state)
        store.prune_snapshots(int(th.get("snapshot_keep_days", 45)))
    return post
