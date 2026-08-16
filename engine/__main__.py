"""CLI: python -m engine <command> --audience <name> [options]

  run       fetch data, build the slot's post, render charts + site
  render    re-render the site from stored posts
  publish   push unpublished posts to channels (site must be live)
  discover  search open markets by regex to help fill a watchlist
  status    print state summary
  reset-breaker  clear a source breaker so the next run tries the source again
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timezone

from .config import load_audience


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="engine")
    ap.add_argument("command", choices=["run", "render", "publish", "discover", "status",
                                        "reset-breaker"])
    ap.add_argument("--audience", required=True)
    ap.add_argument("--slot", default="auto")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="run a slot even if state says it is done")
    ap.add_argument("--post", help="publish only this post id")
    ap.add_argument("--query", help="discover: regex over title/subtitle")
    ap.add_argument("--source", help="reset-breaker: source name (default: every source)")
    ap.add_argument("--now", help="override UTC now, ISO-8601 (testing)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    aud = load_audience(a.audience)
    now = datetime.fromisoformat(a.now).astimezone(timezone.utc) if a.now else datetime.now(timezone.utc)

    if a.command == "run":
        from .render import render_site
        from .run import run
        post = run(aud, slot=a.slot, now=now, dry_run=a.dry_run, force=a.force)
        if not a.dry_run:
            render_site(aud, now)
        if post:
            print(json.dumps({"post": post.id, "chars": len(post.text), "chart": post.chart}, indent=1))
            print("----\n" + post.text + "\n----")
        else:
            print(json.dumps({"post": None}))
        return 0
    if a.command == "render":
        from .render import render_site
        print(render_site(aud, now))
        return 0
    if a.command == "publish":
        from .publish import publish
        res = publish(aud, only_post=a.post, dry_run=a.dry_run)
        print(json.dumps(res, indent=1))
        bad = [c for p in res.values() for c, r in p.items() if not r.get("ok")]
        return 0  # channel failures are logged, never fail the job
    if a.command == "discover":
        from .http import Http
        from .sources import build_sources
        rx = re.compile(a.query or ".", re.I)
        for s in build_sources(aud.sources, Http("attention-discover/0.1")):
            for r in s.fetch_open():
                if rx.search(f"{r.title} {r.subtitle} {r.event_ticker}") and (r.volume_24h or 0) > 0:
                    print(f"{s.name:<11} {r.ticker:<60} {r.title[:60]!r} sub={r.subtitle[:30]!r} yes={r.yes_price} vol24={r.volume_24h:.0f} close={r.close_time}")
        return 0
    if a.command == "reset-breaker":
        from .state import Store
        store = Store(aud.data_dir)
        st = store.load_state()
        breakers = st.get("breakers") or {}
        names = [a.source] if a.source else list(breakers)
        cleared = []
        for name in names:
            b = breakers.get(name)
            if b is None:
                print(f"no breaker for {name!r}; known: {sorted(breakers)}", file=sys.stderr)
                continue
            b.update({"failures": 0, "disabled": False, "disabled_at": None})
            cleared.append(name)
        store.save_state(st)
        print(json.dumps({"cleared": cleared, "breakers": breakers}, indent=1))
        return 0 if cleared else 1
    if a.command == "status":
        from .state import Store
        st = Store(aud.data_dir).load_state()
        print(json.dumps({k: st.get(k) for k in ("last_run", "breakers", "slots_done")}, indent=1)[-3000:])
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
