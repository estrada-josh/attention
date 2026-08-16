# attention site Worker

One generic Cloudflare Worker. It serves one audience site per deploy.

The site is a static folder in a public GitHub repo. GitHub Actions rebuilds the
folder, commits it, then calls `POST /hooks/sync` with the new commit SHA. The
Worker saves the SHA in KV and proxies `raw.githubusercontent.com` pinned to that
SHA. The pin skips the ~5 minute raw cache on a branch name.

## Routes

| Route | Result |
|---|---|
| `POST /hooks/sync` | Needs `Authorization: Bearer $SYNC_TOKEN` and body `{"sha":"<40-hex>"}`. Send `{"sha":"main"}` to unpin. |
| `GET /health.json` | Current SHA, sync time, repo, site root. `no-store`. |
| `GET /.well-known/atproto-did` | 302 to Bridgy Fed. It gives the site a Bluesky handle. |
| any other `GET`/`HEAD` | Static file from the repo. |

## Config

Set these in `audiences/<name>/wrangler.jsonc`.

- `STATE` — KV namespace binding. Keys: `sha`, `synced_at`.
- `SYNC_TOKEN` — secret. Set it with `wrangler secret put`.
- `REPO` — `owner/name`.
- `SITE_ROOT` — repo-relative site folder.
- `DEFAULT_BRANCH` — branch to serve when KV holds no SHA.
- `SITE_URL` — public site URL.
- `BRIDGY_WEB_ID` — bare domain for Bridgy Fed.

## Add a new audience

1. Copy `worker/wrangler.base.jsonc` to `audiences/<name>/wrangler.jsonc`.
2. Change `name`, the route `pattern`, and every value under `vars`.
3. Run `npx wrangler kv namespace create STATE`. Paste the id into the config.
4. Run `npx wrangler secret put SYNC_TOKEN --config audiences/<name>/wrangler.jsonc`.
5. Run `npx wrangler deploy --config audiences/<name>/wrangler.jsonc`.

Run `npm install` in `worker/` first. Run `npm run check` to type check.

## Test

`src/index.ts` exports the pure helpers `mapPath`, `contentTypeFor`,
`extensionOf`, and `isUnsafePath`. Import them to test path rules without a
network call. To test the whole Worker against a real repo, run:

```
npx wrangler dev --config audiences/<name>/wrangler.jsonc --local \
  --var SYNC_TOKEN:t --var REPO:owner/name --var SITE_ROOT:folder
```
