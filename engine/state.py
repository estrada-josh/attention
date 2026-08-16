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
        path = self.snapshot_dir(venue) / f"{_ts_tag(when)}.csv.gz"
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=SNAP_FIELDS)
        w.writeheader()
        for r in rows:
            d = r.to_dict()
            w.writerow({k: (round(d[k], 4) if isinstance(d[k], float) else d[k]) for k in SNAP_FIELDS})
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write(buf.getvalue())
        return path

    def list_snapshots(self, venue: str) -> list[tuple[datetime, Path]]:
        out = []
        for p in self.snapshot_dir(venue).glob("*.csv.gz"):
            try:
                out.append((parse_ts(p.name.split(".")[0]), p))
            except ValueError:
                continue
        return sorted(out)

    def load_snapshot(self, path: Path) -> list[MarketRow]:
        venue = path.parent.name
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return [MarketRow.from_dict({"venue": venue, "title": "", **d}) for d in csv.DictReader(f)]

    def snapshot_at_or_before(self, venue: str, when: datetime,
                              max_age: timedelta | None = None) -> tuple[datetime, list[MarketRow]] | None:
        """The newest snapshot taken at or before `when` (optionally not older than max_age)."""
        best = None
        for ts, p in self.list_snapshots(venue):
            if ts <= when:
                best = (ts, p)
        if best is None:
            return None
        if max_age is not None and when - best[0] > max_age:
            return None
        return best[0], self.load_snapshot(best[1])

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
