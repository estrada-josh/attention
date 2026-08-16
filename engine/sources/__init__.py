"""Source plugins. Register new sources in REGISTRY."""
from .base import Source
from .kalshi import KalshiSource
from .polymarket import PolymarketSource

REGISTRY: dict[str, type[Source]] = {
    "kalshi": KalshiSource,
    "polymarket": PolymarketSource,
}


def build_sources(cfgs: list[dict], http) -> list[Source]:
    out = []
    for cfg in cfgs:
        if not cfg.get("enabled", True):
            continue
        cls = REGISTRY.get(cfg["type"])
        if cls is None:
            raise SystemExit(f"unknown source type {cfg['type']!r}; known: {sorted(REGISTRY)}")
        out.append(cls(cfg, http))
    return out
