# attention — an autonomous audience engine

`attention` runs unattended and builds an audience for a data-driven feed. It pulls public
data, turns it into short honest posts with charts, publishes a static site with feeds, and
pushes each post to social channels. Nobody writes the posts. Nobody has to log in.

One **audience** = one folder under `audiences/<name>/` with an `audience.yml`. The engine
is generic; the first audience is [OddsDrift](audiences/oddsdrift/) (prediction-market odds).

## How it works

```
sources  ->  shapes  ->  site (HTML + Atom + JSON, microformats2)  ->  channels
kalshi        movers      audiences/<name>/site/                        bridgy   (-> Bluesky + fediverse)
polymarket    settled     served by a tiny Cloudflare Worker             nostr
              watchlist   (proxies the committed folder by commit SHA)   mastodon (optional)
              scorecard
```

- **Sources** (`engine/sources/`) return normalized `MarketRow`s from public APIs (no keys).
- **Shapes** (`engine/shapes/`) turn a run into one `Post` (text <= 280 chars, table, chart kind).
- **Render** (`engine/render.py`) writes the site with h-card / h-feed / h-entry markup.
- **Channels** (`engine/channels/`) publish a live post: Bridgy Fed webmention, Nostr note, Mastodon status.
- **Scheduler**: GitHub Actions (`.github/workflows/publish.yml`). Storage of record: this git repo.
- **Site**: `worker/` — a Cloudflare Worker that serves `audiences/<name>/site/` at a custom
  domain. The pipeline uploads the changed files into KV (`engine/sync_site.py`), so the repo
  may stay private. See `worker/README.md` for the upload contract.

## Run locally

```
uv sync --extra dev && npm install
uv run python -m engine run --audience oddsdrift --slot am --force   # fetch, build, render
uv run python -m engine render --audience oddsdrift                  # re-render only
uv run python -m engine publish --audience oddsdrift --dry-run       # what would be published
uv run python -m engine discover --audience oddsdrift --query "senate"
uv run pytest
```

## Add a new audience

1. Copy `audiences/oddsdrift/` to `audiences/<name>/`. Edit `audience.yml`: names, domain,
   sources, shapes (slots + UTC times), channels, thresholds, follow links. Replace `assets/`.
2. Add the new slot times to the cron list in `.github/workflows/publish.yml`. A slot with no
   matching cron entry never posts; `uv run pytest` fails on that, and the workflow warns.
3. Deploy the site: copy `audiences/oddsdrift/wrangler.jsonc`, change name/domain/`SITE_ROOT`,
   create a KV namespace, set `SYNC_TOKEN` (every audience Worker shares the one Actions
   secret `SYNC_TOKEN`), `npx --prefix worker wrangler deploy --config audiences/<name>/wrangler.jsonc`.
4. Bridge it BEFORE the first run: `curl -X POST -d "url=<domain>" https://fed.brid.gy/web-site`.
   A webmention for a domain Bridgy Fed does not know yet returns 400 "No user found".
   The Bluesky handle stays `<domain>.web.brid.gy` until you press the set-handle button on
   `https://fed.brid.gy/web/<domain>`; the `/.well-known/atproto-did` redirect alone does not
   change it. `audience.yml` holds the handle as literal text in `follow_links`.
5. Add a source, shape, or channel by dropping a module in the matching folder and registering it.

## Principles

- Deterministic content: templates plus arithmetic on public data. No LLM in the loop.
- Bot disclosure on every surface. No replies, follows, likes, or mentions. Ever.
- Data is CC BY 4.0 (`DATA_LICENSE`). Code is MIT (`LICENSE`).

## Layout

```
engine/            the reusable engine (Python 3.11)
engine/channels/nostr_publish.mjs   Nostr publisher (Node)
worker/            Cloudflare Worker that serves an audience's site
audiences/<name>/  audience.yml, about.md, assets/, data/ (snapshots, ledger, posts), site/ (generated)
tools/             agentmail.mjs (inbox + verification mail), mastodon.mjs (API signup + posting)
```
