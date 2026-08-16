"""Audience configuration: audiences/<name>/audience.yml -> Audience."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
AUDIENCES_DIR = ROOT / "audiences"


@dataclass
class Audience:
    name: str
    display_name: str
    domain: str
    site_url: str
    description: str
    contact_email: str
    repo: str                       # owner/name on GitHub
    site_root: str                  # repo-relative path of the generated site
    tags: list[str]
    sources: list[dict]             # [{type: kalshi, ...}]
    shapes: list[dict]              # [{type: movers, slot: am, at: "13:17", ...}]
    channels: list[dict]            # [{type: nostr, ...}]
    thresholds: dict
    raw: dict = field(default_factory=dict)

    @property
    def dir(self) -> Path:
        return AUDIENCES_DIR / self.name

    @property
    def data_dir(self) -> Path:
        return self.dir / "data"

    @property
    def site_dir(self) -> Path:
        return self.dir / "site"

    @property
    def assets_dir(self) -> Path:
        return self.dir / "assets"

    def channel(self, type_: str) -> dict | None:
        for c in self.channels:
            if c.get("type") == type_:
                return c
        return None

    def shape(self, slot: str) -> dict | None:
        for s in self.shapes:
            if s.get("slot") == slot:
                return s
        return None


def load_audience(name: str) -> Audience:
    path = AUDIENCES_DIR / name / "audience.yml"
    if not path.exists():
        raise SystemExit(f"no audience config at {path}")
    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    required = ("display_name", "domain", "repo")
    missing = [k for k in required if not raw.get(k)]
    if missing:
        raise SystemExit(f"{path}: missing required keys {missing}")
    domain = raw["domain"]
    return Audience(
        name=name,
        display_name=raw["display_name"],
        domain=domain,
        site_url=raw.get("site_url") or f"https://{domain}",
        description=raw.get("description", ""),
        contact_email=raw.get("contact_email", ""),
        repo=raw["repo"],
        site_root=raw.get("site_root") or f"audiences/{name}/site",
        tags=list(raw.get("tags") or []),
        sources=list(raw.get("sources") or []),
        shapes=list(raw.get("shapes") or []),
        channels=list(raw.get("channels") or []),
        thresholds=dict(raw.get("thresholds") or {}),
        raw=raw,
    )
