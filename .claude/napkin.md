# Napkin — attention

Running notes for this repo. Read before doing anything; append as you learn.
Rotation: keep near 150 lines; move resolved notes to `.claude/napkin-archive/YYYY-MM.md`.

## WHAT THIS PROJECT IS

An autonomous audience-building mechanism. Josh gave one brief (2026-08-16):
"research and build a mechanism that can build me an audience; topic is open;
use the tools you have; sign up for services with the AgentMail API; make all
the decisions; do not wait on me." No human steps: no phone, no CAPTCHA, no card.

## Corrections
| Date | Source | What Went Wrong | What To Do Instead |
|------|--------|----------------|-------------------|
| 2026-08-16 | user | Bash reads of `.env` files got denied | Josh allowed bash commands for .env files mid-session; proceed |

## User Preferences
- Josh wants zero involvement. Find another way before asking. (2026-08-16)
- Deploys to Cloudflare are pre-authorized (assay precedent, 2026-08-09).
- REUSABLE ENGINE (Josh, 2026-08-16): build so it can grow OTHER audiences on OTHER
  platforms later. Pluggable sources + pluggable channels + one config per audience.
  The first topic/channel set is only the first instance, never hard-wired.

## Verified facts (2026-08-16)
- AgentMail org key lives in `.env` (copied from assay). Free tier: 3 inboxes/org;
  `assay@` and `squeeze-watcher@` exist → ONE slot left. No custom domains.
- Cloudflare: wrangler OAuth logged in (estrada.josh@gmail.com, acct
  60caaa34483e66aa91d7cd45ea4fcfe3, workers.dev subdomain `estrada-josh`).
  Zones: joshestrada.com, hushbug.dev, clickswar.com, kibitzshare.com,
  coastal-spotlight.com, bellaefloraservices.com. Plan level unknown (assume free).
- GitHub CLI: logged in as estrada-josh (repo scope).
- Higgsfield: ~66 credits, no TikTok connected → not a lever.
- bsky.social `describeServer`: phoneVerificationRequired=true → API signup blocked.
  Community PDSes probed (witchcraft, ripperoni, zio.blue, mozzius): invite codes.
  blacksky.app: phone=true. → Bluesky needs Bridgy Fed (Mastodon→Bluesky) or other path.
- X/Twitter: infeasible (phone + pay-per-use API) — assay lesson.

## Patterns That Work
- Probe `describeServer` before assuming a PDS allows signup.

## Patterns That Don't Work
- (none yet)
