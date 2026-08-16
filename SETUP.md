# SETUP — the three unlock steps, then it runs itself

> **Status 2026-08-16 21:50 UTC (5:50 PM EDT): ALL STEPS DONE.** Repo public, Worker live,
> workflows pushed and scheduled, first Actions run green, Bridgy bridge active, Bluesky
> handle `@odds-drift.com` with 3 posts + charts, Nostr live. The rest of this
> file is the runbook for the next audience.

State on 2026-08-16: code, data pipeline, site, Worker, workflows, secrets, and the
AgentMail inbox are done. Three actions were blocked by the Claude Code permission
classifier (they create public surfaces). Run them once from the repo root. Each takes
under a minute. After that, nothing needs a human.

## 1. Give the GitHub CLI the `workflow` scope, then push the workflows

```
gh auth refresh -h github.com -s workflow      # opens a device-code login in the browser
git push origin main                             # pushes the two local "ci:" workflow commits
```

Verify: `gh workflow list --repo estrada-josh/attention` shows `publish` and `healthcheck`.

## 2. Deploy the site Worker and set its secret

```
npm install --prefix worker
npx --prefix worker wrangler deploy --config audiences/oddsdrift/wrangler.jsonc
npx --prefix worker wrangler secret put SYNC_TOKEN --config audiences/oddsdrift/wrangler.jsonc
```

Paste the value of `SYNC_TOKEN` when asked. It is the same value stored as the Actions
secret `SYNC_TOKEN` (`gh secret list`). If you lost it: generate a new one with
`python3 -c "import secrets; print(secrets.token_urlsafe(32))"` and set it in both places
(`gh secret set SYNC_TOKEN` and `wrangler secret put SYNC_TOKEN`).

Verify: `curl -sI https://odds-drift.com/health.json | head -1` returns 200.

## 3. Bridge the site FIRST, then run the pipeline

Order matters. `POST /webmention` returns 400 "No user found for domain" when the site was
never enabled, so the first post would fail and Bridgy Fed would bridge the feed instead.

```
curl -sS -X POST -d "url=odds-drift.com" https://fed.brid.gy/web-site
gh workflow run publish.yml -f audience=oddsdrift -f slot=am -f force=true
gh run watch
```

`/web-site` creates the Bridgy Fed user and polls `feed.xml` once. Entries that already sit
in the feed get bridged from that poll; the first workflow run then sends an Update for them.
Every entry carries `activity:object-type = note`, so a feed-poll entry becomes a note, not
an article.

The run fetches data, builds a post, uploads the site to the Worker, and publishes
(Bridgy webmention + Nostr). Bridgy Fed then creates `@odds-drift.com.web.brid.gy`
on Bluesky. Check: `https://bsky.app/profile/odds-drift.com.web.brid.gy` and
`https://fed.brid.gy/web/odds-drift.com` (status page names any markup fix).

Optional, custom Bluesky handle. The `/.well-known/atproto-did` redirect alone does not
change the handle. The account stays `@odds-drift.com.web.brid.gy` until you open
`https://fed.brid.gy/web/odds-drift.com` and press the set-handle button next to
the Bluesky account. Verify with `curl -sL https://odds-drift.com/.well-known/atproto-did`
(it prints `did:plc:...`) and `https://bsky-debug.app/handle?handle=odds-drift.com`.
After the switch, update `follow_links` in `audiences/oddsdrift/audience.yml`, which holds the
handle as literal text.

Optional, recommended: make the repo public so the CC BY data is citable and GitHub becomes
a channel: `gh repo edit estrada-josh/attention --visibility public --accept-visibility-change-consequences`.
The Worker can then also fall back to raw.githubusercontent.com. That fallback is off while
the repo is private: set `"RAW_FALLBACK": "true"` in `audiences/oddsdrift/wrangler.jsonc` and
redeploy after the repo becomes public. The site itself does not need it; every file is in KV.

Optional: to let future Claude Code sessions do these steps hands-off, allow
`Bash(npx wrangler *)`, `Bash(npx --prefix worker wrangler *)`, and `Bash(gh *)` in
`.claude/settings.local.json` (this is what the assay repo does).

## What runs afterwards

- `publish.yml` cron: 13:17 UTC movers (9:17 AM EDT / 8:17 AM EST), 17:17 UTC midterms board
  Mon–Sat (1:17 PM EDT / 12:17 PM EST), 22:17 UTC settled (6:17 PM EDT / 5:17 PM EST),
  Sunday 15:17 UTC scorecard (11:17 AM EDT / 10:17 AM EST). Retry minute :47.
- `healthcheck.yml` daily 14:30 UTC (10:30 AM EDT / 9:30 AM EST): metrics.csv, one issue after
  two failing days, re-enables the schedule.
- Contact inbox: `oddsdrift@agentmail.to` (read with `node tools/agentmail.mjs list oddsdrift@agentmail.to`).
