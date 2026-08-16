"""Channel plugins: publish a Post that is already live on the site.

Each channel gets (audience, post, env, prev) and returns a dict record. `prev`
is the record the same channel wrote for the same post on an earlier run, or an
empty dict. A channel must be idempotent per post id (the engine records channel
results in state.json['published'][post_id][channel]).

Record keys the engine reads: `ok` (True stops every later attempt) and
`gave_up` (True also stops later attempts). Add new channels here.
"""
from .base import Channel
from .bridgy import BridgyChannel
from .nostr import NostrChannel
from .mastodon import MastodonChannel

REGISTRY: dict[str, type[Channel]] = {
    "bridgy": BridgyChannel,
    "nostr": NostrChannel,
    "mastodon": MastodonChannel,
}


def build_channels(cfgs: list[dict]) -> list[Channel]:
    out = []
    for cfg in cfgs:
        if not cfg.get("enabled", True):
            continue
        cls = REGISTRY.get(cfg["type"])
        if cls is None:
            raise SystemExit(f"unknown channel type {cfg['type']!r}; known: {sorted(REGISTRY)}")
        out.append(cls(cfg))
    return out
