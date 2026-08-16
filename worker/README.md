# attention site Worker

One generic Cloudflare Worker. It serves one audience site per deploy.

The site is a static folder in a GitHub repo. The repo may be private: GitHub
Actions rebuilds the folder, commits it, and then uploads every changed file
into KV (`engine/sync_site.py`). The Worker serves the files from KV.

When the repo is public you can also turn on the `raw.githubusercontent.com`
fallback with the var `RAW_FALLBACK: "true"`. The Worker then proxies raw for a
file that is not in KV, pinned to the commit SHA the upload sent. The pin skips
the ~5 minute raw cache on a branch name. The fallback stays off by default
because a private repo answers 404 for every request.

## Routes

| Route | Result |
|---|---|
| `GET /hooks/manifest` | Needs `Authorization: Bearer $SYNC_TOKEN`. Returns the stored manifest, `{"<path>": "<sha256>"}`. |
| `POST /hooks/upload` | Needs the same bearer. Uploads files into KV. See the contract below. |
| `POST /hooks/sync` | Needs the same bearer, body `{"sha":"<40-hex>"}`. Pins the raw fallback. Send `{"sha":"main"}` to unpin. |
| `GET /health.json` | Current SHA, sync time, repo, site root. `no-store`. 503 with `{"ok":false,"error":"kv_unavailable"}` when KV fails. |
| `GET /.well-known/atproto-did` | 302 to Bridgy Fed. It is needed for a custom Bluesky handle, which you still turn on from the fed.brid.gy user page. |
| any other `GET`/`HEAD` | Static file from KV, then the optional raw fallback, then a 404 cached for 60 s. |

## Upload contract (`POST /hooks/upload`)

`engine/sync_site.py` is the only client. One sync sends several requests.

1. `GET /hooks/manifest` -> the path to sha256 map of what KV holds.
2. Content requests: `{"files":[{"path":"/index.html","b64":"...","sha256":"..."}]}`.
   They write `file:<path>` keys only. Pages, feeds, the sitemap, and charts go
   before `/data/*`, so a large data file can never block a new post page.
3. Final request: `{"files":[], "delete":["/old.html"], "manifest":{...},
   "sha":"<40-hex>", "final":true}`. It writes the meta keys.

Limits: `MAX_UPLOAD_BODY` = 2 MiB per request, `MAX_UPLOAD_FILES` = 40 files per
request. The client skips any single file above 1.4 MB raw and warns, because
such a file can never fit in a request body.

The meta keys move on the final request only. KV allows one write per second
per key, so a meta write in every chunk can fail with 429 in the middle of a
sync. The client sends the full merged manifest, so the Worker never does a
read-modify-write of the manifest either.

KV keys:

- `file:<path>` — one site file. Metadata: `{ct, sha256}`.
- `manifest` — JSON, path to sha256, for the client diff.
- `sha` — pinned commit SHA (raw fallback and `/health.json`).
- `synced_at`, `version` — last sync time. `version` is the edge cache buster.

## Config

Set these in `audiences/<name>/wrangler.jsonc`.

- `STATE` — KV namespace binding. Keys: see above.
- `SYNC_TOKEN` — secret. Set it with `wrangler secret put`.
  `.github/workflows/publish.yml` passes one `secrets.SYNC_TOKEN` to every
  audience, so every audience Worker needs the same secret value.
- `REPO` — `owner/name`.
- `SITE_ROOT` — repo-relative site folder.
- `DEFAULT_BRANCH` — branch to serve when KV holds no SHA.
- `SITE_URL` — public site URL.
- `BRIDGY_WEB_ID` — bare domain for Bridgy Fed.
- `RAW_FALLBACK` — `"true"` only when the repo is public. Default off.

## Add a new audience

1. Copy `worker/wrangler.base.jsonc` to `audiences/<name>/wrangler.jsonc`.
2. Change `name`, the route `pattern`, and every value under `vars`.
3. Run `npx --prefix worker wrangler kv namespace create STATE`. Paste the id into the config.
4. Run `npx --prefix worker wrangler secret put SYNC_TOKEN --config audiences/<name>/wrangler.jsonc`.
5. Run `npx --prefix worker wrangler deploy --config audiences/<name>/wrangler.jsonc`.

Run `npm install --prefix worker` first. Wrangler lives in `worker/node_modules`,
so always use `npx --prefix worker`. Run `npm run check` in `worker/` to type check.

## Test

`src/index.ts` exports the pure helpers `mapPath`, `contentTypeFor`,
`extensionOf`, and `isUnsafePath`. Import them to test path rules without a
network call. To test the whole upload and serve path locally:

```
npx --prefix worker wrangler dev --config audiences/<name>/wrangler.jsonc \
  --port 8787 --var SYNC_TOKEN:devtoken123
SYNC_TOKEN=devtoken123 uv run python -m engine.sync_site \
  --audience <name> --base http://127.0.0.1:8787
```
