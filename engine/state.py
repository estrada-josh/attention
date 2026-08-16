"""Storage of record for one audience: files under audiences/<name>/data/.

Layout:
  data/state.json                    slots done, source breakers, misc
  data/posts.json                    post index, newest first (site input)
  data/snapshots/<venue>/<ts>.csv.gz one file per run per venue (rolling window)
  data/resolutions.csv               every settled market seen (append, dedup)
  data/metrics.csv                   daily healthcheck rows
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .model import MarketRow, Post

log = logging.getLogger("engine.state")

# Snapshots hold only what the engine reads back (24h reference prices,
# price-before-settlement, liquidity filters). Titles/urls come from live data.
SNAP_FIELDS = ["ticker", "yes_price", "volume_24h", "open_interest", "liquidity", "category", "close_time"]
RES_FIELDS = ["venue", "ticker", "title", "subtitle", "result", "settled_at",
              "close_time", "last_price_before", "price_24h_before",
              "price_7d_before", "volume_24h", "url", "recorded_at"]


def _ts_tag(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H%M")


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H%M").replace(tzinfo=timezone.utc)


class Store:
    def __init__(self, data_dir: Path):
        self.dir = data_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "snapshots").mkdir(exist_ok=True)
        # Per-run caches. One run asks for a reference price tens of thousands of
        # times; without these, every ask re-globs a directory and gunzips a file.
        self._list_cache: dict[str, list[tuple[datetime, Path]]] = {}
        self._price_cache: dict[Path, dict[str, float]] = {}

    def _drop_caches(self) -> None:
        self._list_cache.clear()
        self._price_cache.clear()

    # ---------------------------------------------------------- state.json
    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    def load_state(self) -> dict:
        if self.state_path.exists():
            return json.loads(self.state_path.read_text())
        return {"slots_done": {}, "breakers": {}, "created": datetime.now(timezone.utc).isoformat()}

    def save_state(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    # ------------------------------------------------------------ snapshots
    def snapshot_dir(self, venue: str) -> Path:
        d = self.dir / "snapshots" / venue
        d.mkdir(parents=True, exist_ok=True)
        return d

    def save_snapshot(self, venue: str, rows: list[MarketRow], when: datetime) -> Path:
        """Write one snapshot file. Writes to a temp file first, then renames, so
        a killed run cannot leave a truncated .csv.gz behind."""
        path = self.snapshot_dir(venue) / f"{_ts_tag(when)}.csv.gz"
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=SNAP_FIELDS)
        w.writeheader()
        for r in rows:
            d = r.to_dict()
            w.writerow({k: (round(d[k], 4) if isinstance(d[k], float) else d[k]) for k in SNAP_FIELDS})
        tmp = path.with_suffix(path.suffix + ".tmp")
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            f.write(buf.getvalue())
        os.replace(tmp, path)
        self._drop_caches()
        return path

    def list_snapshots(self, venue: str) -> list[tuple[datetime, Path]]:
        if venue in self._list_cache:
            return self._list_cache[venue]
        out = []
        for p in self.snapshot_dir(venue).glob("*.csv.gz"):
            try:
                out.append((parse_ts(p.name.split(".")[0]), p))
            except ValueError:
                continue
        self._list_cache[venue] = sorted(out)
        return self._list_cache[venue]

    def load_snapshot(self, path: Path) -> list[MarketRow]:
        venue = path.parent.name
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return [MarketRow.from_dict({"venue": venue, "title": "", **d}) for d in csv.DictReader(f)]

    def snapshot_prices(self, path: Path) -> dict[str, float]:
        """ticker -> yes_price for one snapshot file, read once per run.
        A damaged file yields an empty map instead of an exception, because the
        caller treats a source exception as a breaker failure."""
        if path in self._price_cache:
            return self._price_cache[path]
        prices: dict[str, float] = {}
        try:
            for r in self.load_snapshot(path):
                if r.yes_price is not None:
                    prices[r.ticker] = r.yes_price
        except (OSError, EOFError, gzip.BadGzipFile) as e:
            log.error("snapshot %s unreadable (%s); treating it as empty", path.name, e)
        self._price_cache[path] = prices
        return prices

    def _newest_at_or_before(self, venue: str, when: datetime,
                             max_age: timedelta | None) -> tuple[datetime, Path] | None:
        best = None
        for ts, p in self.list_snapshots(venue):
            if ts <= when:
                best = (ts, p)
        if best is None:
            return None
        if max_age is not None and when - best[0] > max_age:
            return None
        return best

    def snapshot_at_or_before(self, venue: str, when: datetime,
                              max_age: timedelta | None = None) -> tuple[datetime, list[MarketRow]] | None:
        """The newest snapshot taken at or before `when` (optionally not older than max_age)."""
        best = self._newest_at_or_before(venue, when, max_age)
        if best is None:
            return None
        return best[0], self.load_snapshot(best[1])

    def price_map_at_or_before(self, venue: str, when: datetime,
                               max_age: timedelta | None = None) -> tuple[datetime, dict[str, float]] | None:
        """The newest snapshot at or before `when`, as a cached ticker -> price map."""
        best = self._newest_at_or_before(venue, when, max_age)
        if best is None:
            return None
        return best[0], self.snapshot_prices(best[1])

    def prune_snapshots(self, keep_days: int = 45) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        n = 0
        for venue_dir in (self.dir / "snapshots").iterdir():
            if not venue_dir.is_dir():
                continue
            for ts, p in self.list_snapshots(venue_dir.name):
                if ts < cutoff:
                    p.unlink()
                    n += 1
        if n:
            self._drop_caches()
        return n

    # ------------------------------------------------------------ resolutions
    @property
    def resolutions_path(self) -> Path:
        return self.dir / "resolutions.csv"

    def load_resolutions(self) -> list[dict]:
        if not self.resolutions_path.exists():
            return []
        with open(self.resolutions_path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def append_resolutions(self, rows: list[dict]) -> int:
        existing = {(r["venue"], r["ticker"]) for r in self.load_resolutions()}
        new = [r for r in rows if (r["venue"], r["ticker"]) not in existing]
        if not new:
            return 0
        write_header = not self.resolutions_path.exists()
        with open(self.resolutions_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=RES_FIELDS, extrasaction="ignore")
            if write_header:
                w.writeheader()
            for r in new:
                w.writerow({k: r.get(k, "") for k in RES_FIELDS})
        return len(new)

    # ---------------------------------------------------------------- posts
    @property
    def posts_path(self) -> Path:
        return self.dir / "posts.json"

    def load_posts(self) -> list[dict]:
        if self.posts_path.exists():
            return json.loads(self.posts_path.read_text())
        return []

    def add_post(self, post: Post) -> None:
        posts = [p for p in self.load_posts() if p["id"] != post.id]
        posts.insert(0, post.to_dict())
        posts.sort(key=lambda p: p["published_at"], reverse=True)
        self.posts_path.write_text(json.dumps(posts, indent=1) + "\n")

    # -------------------------------------------------------------- metrics
    def append_metrics(self, row: dict) -> None:
        path = self.dir / "metrics.csv"
        fields = list(row.keys())
        write_header = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            if write_header:
                w.writeheader()
            w.writerow(row)
