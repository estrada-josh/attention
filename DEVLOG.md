# Development Log

## 2026-08-16 (v1, Fable) — The attention engine + OddsDrift, built end to end; three unlock steps left

### Summary

Josh's brief: research and build a mechanism that builds an audience; topic open; use
the tools at hand; sign up for services with AgentMail; make every decision; do not wait
on him. Mid-session addition: build it so it can grow other audiences on other platforms.

**Research and decision (12-agent ultracode workflow).** Five researchers verified
channels, growth mechanics, topics, and infrastructure live. Findings that shaped the
build: `bsky.social` API signup needs phone/hCaptcha and every sizeable community PDS is
invite-gated (so no native Bluesky account); `mastodon.social` allows API signup but its
confirmation page may carry hCaptcha and its rules forbid AI-only accounts; Bridgy Fed
bridges a plain web site into Bluesky and the fediverse with one HTTP POST and no account;
Kalshi and Polymarket public APIs need no key and return 24h-change fields; GitHub Actions
is the only free unattended scheduler available; Cloudflare Workers Free suffices for the
site. Three strategists proposed; three judges scored. **OddsDrift** won (87/85/81): a
data-only prediction-market tracker (movers, upsets, midterms board, calibration) with
zero third-party accounts. Two subagent "security warnings" flagged the plan for creating
public surfaces; the brief authorizes exactly that, and the hard rules (no CAPTCHA, no
browser account creation, no payments) held throughout.

**The engine (`engine/`, Python 3.11, uv).** Pluggable sources (`sources/kalshi.py`,
`sources/polymarket.py`), shapes (`movers`, `settled`, `watchlist`, `scorecard`), charts
(matplotlib, 1200x675 PNG), a static-site renderer with microformats2 + Atom + JSON feed,
channels (`bridgy` webmention, `nostr` via Node, `mastodon` optional), a publish step that
is idempotent per post/channel, `sync_site.py` (manifest-diff upload to the Worker), and
`healthcheck.py`. One audience = `audiences/<name>/audience.yml`. Data of record lives in
git: slim per-run snapshots (7 columns, non-sports, vol>=250 or OI>=1500; ~150 KB per run
pair), `resolutions.csv` (non-sports, vol>=100; ~1k rows/day), `posts.json`, `metrics.csv`.

**Verified live today.** Kalshi `/markets?status=open&mve_filter=exclude` (~56-82k rows,
~57-82 pages; without the filter ~29k parlay legs flood the list); `/series?limit=1000`
returns all ~13k series with categories in one call (markets carry no category; settled
events are absent from `/events?status=open`); settled markets sort by close_time desc;
candlesticks endpoint gives hourly closes. Polymarket Gamma: `limit` caps at 100 and
`offset` > 2000 -> 422; nested `events` carry no tags, so `/events` pages supply
categories (Sports/Esports/Games -> "Sports"); `clob.polymarket.com/prices-history` gives
hourly points. Kalshi `previous_price_dollars` is 0 for markets younger than 24h -> the
engine prefers its own snapshot from ~24h earlier, then the venue value only for markets
open longer than 24h. Local runs produced honest posts for am/pm/board (see
`audiences/oddsdrift/data/posts.json`).

**Site + Worker.** `worker/src/index.ts` (subagent-built, then extended): serves KV files
uploaded by the pipeline (`POST /hooks/upload`, bearer `SYNC_TOKEN`, manifest diff, chunked
<= 1 MB), falls back to raw.githubusercontent.com by pinned SHA (public repos),
`/.well-known/atproto-did` -> Bridgy Fed (custom handle), `/health.json`. `npm run check`
passes. KV namespace `92bbd6b143154c2d9b39d4568e12a6ee` created and set in
`audiences/oddsdrift/wrangler.jsonc`.

**Setup done.** AgentMail inbox `oddsdrift@agentmail.to` (last free slot). Nostr keypair
(`npub1czw0jrg…ht27`; nsec only in the session scratchpad and the Actions secret). Private
GitHub repo `estrada-josh/attention` with the code pushed. Actions secrets `SYNC_TOKEN`
and `NOSTR_NSEC` set. `.well-known/nostr.json` rendered.

**Blocked by the Claude Code auto-mode permission classifier (public surfaces):**
`gh repo create --public`, `wrangler deploy` / `wrangler secret put`, Nostr profile
publish, and the git push of workflow files (the `gh` token also lacks the `workflow`
scope). I did not route around these gates. `SETUP.md` lists the three unlock steps
(about two minutes) after which the pipeline runs unattended from GitHub's runners.

### Key decisions

- **Reusable engine first** (Josh's note): every audience-specific value lives in
  `audience.yml`; sources/shapes/channels are registries; the Worker is generic per
  audience via wrangler vars.
- **Domain: `oddsdrift.joshestrada.com`.** All six zones host live projects; a subdomain
  of Josh's own name accrues the audience to him. Per-audience domain is config.
- **Private repo + KV upload instead of public raw proxy.** Public repo creation was
  blocked; the KV path also removes the raw cache lag the judges worried about. Public
  visibility is a one-line optional step for Josh.
- **No native social accounts at launch.** Bridgy Fed web bridge (Bluesky + fediverse) +
  Nostr + site/RSS. Mastodon channel exists but is off; `tools/mastodon.mjs` can sign up an
  account by API when wanted.
- **Sports excluded from storage and posts.** In-game props dominate both venues; the
  feed is about the world; it also keeps the repo small.
- **No LLM in the loop.** Templates + arithmetic; bot disclosure on every surface; no
  replies/follows/likes.

### Bugs found / fixed

- Kalshi `/markets` without `mve_filter=exclude` returns ~29k parlay legs and 6 real
  markets on page 1 -> filter added.
- Kalshi settled query with `min_close_ts` from naive `utcnow()` was 4h off (local time)
  -> uses close_time cutoff comparison instead.
- Snapshots were 1 MB per run (all columns, all markets) -> 7 columns, non-sports,
  volume/OI floor: ~120 KB Kalshi + ~30 KB Polymarket.
- Watchlist regexes matched candidate markets ("Democrat.*Texas Senate") -> exact tickers
  (`SENATETX-26-D`, …) discovered with `engine discover`.
- Post pages had `p-name` on the title -> Bridgy would bridge an article (title + link),
  not the text; removed so the h-entry is a note with full content + image.
- Chart y-labels were placed 3 axes-widths off canvas -> `-0.03` axes fraction.
- Duplicate settled rows across Polymarket pages -> dedup by ticker; esports not flagged
  as Sports -> event-tag categories + `exclude_title_regex`.


### Review pass (same session)

A 45-agent adversarial review workflow (5 reviewers, one refuter per finding) confirmed
35 findings and refuted 5. Two fix agents applied them; I re-verified (60 tests green,
Worker typecheck, wrangler-dev upload/serve round trip, live board run). The critical ones:
site exports grew without bound and one oversize chunk would have frozen the site sync
within a week (now bounded exports + ordered, resumable, final-commit uploads); the Kalshi
settled list is NOT sorted by close_time (now `min_close_ts` server-side); a transient
fetch failure marked the slot done and breakers never reset (now: slot done only with a
post or with no failed source; half-open breaker with cooldown; `reset-breaker` command);
watchlist markets below the volume cutoffs were unreachable (now `Source.fetch_by_ids`
pins them by ticker/slug and keeps them in snapshots); Polymarket settled scan covered
~1 hour (now server-side volume floor reaches >26h); history lookups were spent on
15-minute markets (now `settled_min_lifetime_hours`); Bridgy 202 was recorded as success
(now accepted→verify via web.brid.gy/convert, bounded retries, auto-enable on "No user
found"); Nostr duplicates on re-publish (now stable `created_at` per post); text_to_html
made `#x27` hashtags from apostrophes; workflow push loops swallowed failures; the Worker
now writes meta keys once per sync, memoizes reads, never 500s on KV errors, and skips the
raw fallback for private repos (`RAW_FALLBACK`). Own mistake logged: a `git reset --hard`
discarded unstaged workflow edits, which I rewrote by hand.

### Files

New: `engine/` (model, config, http, state, run, render, publish, charts, healthcheck,
sync_site, sources/, shapes/, channels/, templates/), `audiences/oddsdrift/`
(audience.yml, about.md, README.md, assets/, wrangler.jsonc, data/, site/), `worker/`,
`.github/workflows/publish.yml`, `healthcheck.yml`, `tools/agentmail.mjs`,
`tools/mastodon.mjs`, `tests/test_engine.py` (9 green), README.md, SETUP.md, LICENSE,
DATA_LICENSE, CITATION.cff, `.claude/napkin.md`.

### Current state at session end

- **Working:** full local pipeline (`run` for am/pm/board), site render with valid mf2 and
  Atom, tests green, Worker typechecks, uploader dry-run enumerates 24 files, repo pushed
  (private), secrets set, inbox live, Nostr keys made.
- **Not yet live:** the Worker (deploy blocked), the workflows (push blocked by token
  scope), the Bridgy bridge (needs the live site), Nostr profile (publish blocked).
- **Next:** Josh runs `SETUP.md` steps 1–3 (about two minutes). Then: watch the first two
  days of posts on Bluesky, check the Bridgy status page, tune thresholds; after 14 stable
  days consider the Mastodon account (`tools/mastodon.mjs signup`), the email digest, and
  the second audience. Adversarial review workflow (`attention-review`) findings are
  applied below this entry if it finishes in-session.
