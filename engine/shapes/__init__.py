"""Shape plugins. Register new shapes in REGISTRY."""
from .base import Shape, Context
from .movers import MoversShape
from .settled import SettledShape
from .watchlist import WatchlistShape
from .scorecard import ScorecardShape

REGISTRY: dict[str, type[Shape]] = {
    "movers": MoversShape,
    "settled": SettledShape,
    "watchlist": WatchlistShape,
    "scorecard": ScorecardShape,
}


def build_shape(cfg: dict) -> Shape:
    cls = REGISTRY.get(cfg["type"])
    if cls is None:
        raise SystemExit(f"unknown shape type {cfg['type']!r}; known: {sorted(REGISTRY)}")
    return cls(cfg)
