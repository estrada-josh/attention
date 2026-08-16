/**
 * attention — generic audience site Worker.
 *
 * One audience site is a static folder in a GitHub repo (public or private).
 * GitHub Actions rebuilds the folder and commits it. Then Actions uploads
 * the changed files to this Worker (POST /hooks/upload) which stores them in
 * KV. Requests are served from KV first. When a file is not in KV and the
 * repo is public, the Worker proxies raw.githubusercontent.com pinned to the
 * SHA sent by POST /hooks/sync.
 *
 * The pin matters. raw.githubusercontent.com caches a branch name for about
 * 5 minutes. A commit SHA is immutable, so raw serves it at once. The SHA also
 * gives the edge cache a new key on every deploy. No cache purge is needed.
 *
 * The Worker is generic. One deploy per audience. All audience values come
 * from wrangler vars.
 */

export interface Env {
  /** KV namespace. It holds the keys `sha` and `synced_at`. */
  STATE: KVNamespace
  /** Bearer token for POST /hooks/sync. Set it with `wrangler secret put`. */
  SYNC_TOKEN: string
  /** GitHub repo as "owner/name". */
  REPO: string
  /** Repo-relative site folder, for example "audiences/oddsdrift/site". */
  SITE_ROOT: string
  /** Branch to serve when KV holds no SHA. */
  DEFAULT_BRANCH: string
  /** Public site URL, for example "https://oddsdrift.joshestrada.com". */
  SITE_URL: string
  /** Bare domain for Bridgy Fed, for example "oddsdrift.joshestrada.com". */
  BRIDGY_WEB_ID: string
}

/** A full git commit SHA. */
const SHA_PATTERN = /^[0-9a-f]{40}$/
/** Largest sync hook body we read, in bytes. */
const MAX_HOOK_BODY = 4096
/** Largest upload body we read, in bytes (files are sent in chunks). */
const MAX_UPLOAD_BODY = 2 * 1024 * 1024
/** Most files in one upload request. Keeps CPU per request small. */
const MAX_UPLOAD_FILES = 40
/** KV key prefix for stored site files. */
const FILE_PREFIX = 'file:'
/** Edge cache seconds for a file served from KV. Uploads bump a version. */
const TTL_KV = 300
/** Edge cache seconds for a pinned commit SHA. The content cannot change. */
const TTL_PINNED = 600
/** Edge cache seconds for a branch name. The content can change. */
const TTL_BRANCH = 60

/**
 * Vars that must hold a non-empty string.
 * SITE_ROOT is not here. An empty SITE_ROOT means the site is at the repo root.
 */
const REQUIRED_VARS = ['REPO', 'DEFAULT_BRANCH', 'SITE_URL', 'BRIDGY_WEB_ID'] as const

/**
 * File extension to Content-Type. The Worker copies no headers from raw,
 * so it sets the type itself.
 */
const CONTENT_TYPES: Record<string, string> = {
  html: 'text/html; charset=utf-8',
  xml: 'application/xml; charset=utf-8',
  json: 'application/json; charset=utf-8',
  css: 'text/css; charset=utf-8',
  js: 'text/javascript; charset=utf-8',
  txt: 'text/plain; charset=utf-8',
  csv: 'text/csv; charset=utf-8',
  svg: 'image/svg+xml',
  png: 'image/png',
  jpg: 'image/jpeg',
  jpeg: 'image/jpeg',
  webp: 'image/webp',
  ico: 'image/x-icon',
  gz: 'application/gzip',
  cff: 'text/plain; charset=utf-8'
}

const NOT_FOUND_HTML =
  '<!doctype html><meta charset="utf-8"><title>404</title>' +
  '<body style="font:16px system-ui;margin:4rem auto;max-width:32rem"><h1>404</h1>' +
  '<p>That page is not here. <a href="/">Go to the home page.</a></p>'

/** Return the lowercase extension of the last path segment, or "". */
export function extensionOf(path: string): string {
  const segment = path.slice(path.lastIndexOf('/') + 1)
  const dot = segment.lastIndexOf('.')
  if (dot < 1) return ''
  return segment.slice(dot + 1).toLowerCase()
}

/** Pick the Content-Type for a repo path. */
export function contentTypeFor(path: string): string {
  const extension = extensionOf(path)
  // The Atom feed needs its own type. Other .xml files stay generic.
  if (path.endsWith('/feed.xml')) return 'application/atom+xml; charset=utf-8'
  return CONTENT_TYPES[extension] ?? 'application/octet-stream'
}

/**
 * Map a request path to a repo file path.
 * "/"          -> "/index.html"
 * "/blog/"     -> "/blog/index.html"
 * "/blog/post" -> "/blog/post.html"
 * "/style.css" -> "/style.css"
 * Paths under /.well-known/ always pass through as-is.
 */
export function mapPath(pathname: string): string {
  if (pathname === '/' || pathname === '') return '/index.html'
  if (pathname.endsWith('/')) return pathname + 'index.html'
  // Well-known files have no extension by design. Never add ".html" to them.
  if (pathname.startsWith('/.well-known/')) return pathname
  if (extensionOf(pathname) === '') return pathname + '.html'
  return pathname
}

/** True when the path tries to escape the site root. */
export function isUnsafePath(pathname: string): boolean {
  if (pathname.includes('..')) return true
  // An attacker can hide ".." behind percent escapes. Decode once and re-check.
  let decoded = pathname
  try {
    decoded = decodeURIComponent(pathname)
  } catch {
    return true // Broken escapes are never a real file.
  }
  return decoded.includes('..')
}

/** Compare two strings without an early exit on the first wrong byte. */
function tokensMatch(given: string, expected: string): boolean {
  if (given.length !== expected.length) return false
  let diff = 0
  for (let i = 0; i < given.length; i++) {
    diff |= given.charCodeAt(i) ^ expected.charCodeAt(i)
  }
  return diff === 0
}

function jsonResponse(body: unknown, status: number, extraHeaders?: Record<string, string>): Response {
  const headers = new Headers({ 'content-type': 'application/json; charset=utf-8' })
  for (const [key, value] of Object.entries(extraHeaders ?? {})) headers.set(key, value)
  return new Response(JSON.stringify(body), { status, headers })
}

function textResponse(body: string, status: number): Response {
  return new Response(body, {
    status,
    headers: { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store' }
  })
}

/**
 * Read the pinned SHA from KV. Fall back to the default branch.
 * The value goes into an upstream URL, so validate it before use.
 */
async function readRef(env: Env): Promise<string> {
  const stored = await env.STATE.get('sha')
  if (stored && SHA_PATTERN.test(stored)) return stored
  return env.DEFAULT_BRANCH
}

/**
 * Build the edge cache key. The SHA goes in a query param, not a fragment,
 * because the Cache API keys on the URL and drops fragments.
 */
function cacheKeyFor(request: Request, ref: string): Request {
  const url = new URL(request.url)
  url.searchParams.set('__ref', ref)
  return new Request(url.toString(), { method: 'GET' })
}

/** True when the request carries the sync bearer token. */
function authorized(request: Request, env: Env): boolean {
  if (!env.SYNC_TOKEN) return false
  const header = request.headers.get('authorization') ?? ''
  const prefix = 'Bearer '
  return header.startsWith(prefix) && tokensMatch(header.slice(prefix.length), env.SYNC_TOKEN)
}

/** Decode standard base64 into bytes. */
function fromBase64(b64: string): Uint8Array {
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i)
  return out
}

interface UploadFile {
  path: string
  b64: string
  sha256: string
}

/**
 * Handle POST /hooks/upload. Body: {"files":[{path,b64,sha256}], "delete":[path], "sha":"<commit>"}.
 * Files go to KV under "file:<path>". The manifest (path -> sha256) is merged.
 * The client sends only changed files (it diffs against GET /hooks/manifest).
 */
async function handleUpload(request: Request, env: Env): Promise<Response> {
  if (!env.SYNC_TOKEN) return jsonResponse({ error: 'sync_not_configured' }, 503)
  if (!authorized(request, env)) return jsonResponse({ error: 'unauthorized' }, 401)
  const declared = Number(request.headers.get('content-length') ?? '0')
  if (declared > MAX_UPLOAD_BODY) return jsonResponse({ error: 'bad_request', detail: 'body too large' }, 400)
  let body: { files?: UploadFile[]; delete?: string[]; sha?: string }
  try {
    body = JSON.parse(await request.text())
  } catch {
    return jsonResponse({ error: 'bad_request', detail: 'body is not JSON' }, 400)
  }
  const files = Array.isArray(body.files) ? body.files : []
  const deletes = Array.isArray(body.delete) ? body.delete : []
  if (files.length > MAX_UPLOAD_FILES) return jsonResponse({ error: 'bad_request', detail: 'too many files' }, 400)

  const manifest: Record<string, string> = JSON.parse((await env.STATE.get('manifest')) ?? '{}')
  const written: string[] = []
  for (const f of files) {
    if (typeof f.path !== 'string' || !f.path.startsWith('/') || isUnsafePath(f.path) || typeof f.b64 !== 'string') continue
    const bytes = fromBase64(f.b64)
    await env.STATE.put(FILE_PREFIX + f.path, bytes, { metadata: { ct: contentTypeFor(f.path), sha256: f.sha256 ?? '' } })
    manifest[f.path] = f.sha256 ?? ''
    written.push(f.path)
  }
  for (const p of deletes) {
    if (typeof p !== 'string' || !p.startsWith('/') || isUnsafePath(p)) continue
    await env.STATE.delete(FILE_PREFIX + p)
    delete manifest[p]
  }
  await env.STATE.put('manifest', JSON.stringify(manifest))
  const syncedAt = new Date().toISOString()
  if (typeof body.sha === 'string' && SHA_PATTERN.test(body.sha)) await env.STATE.put('sha', body.sha)
  await env.STATE.put('synced_at', syncedAt)
  // A new version key makes the edge cache miss for every path.
  await env.STATE.put('version', syncedAt)
  return jsonResponse({ ok: true, written: written.length, deleted: deletes.length, manifest_size: Object.keys(manifest).length, synced_at: syncedAt }, 200, { 'cache-control': 'no-store' })
}

/** Handle GET /hooks/manifest: the path -> sha256 map of files stored in KV. */
async function handleManifest(request: Request, env: Env): Promise<Response> {
  if (!authorized(request, env)) return jsonResponse({ error: 'unauthorized' }, 401)
  const manifest = (await env.STATE.get('manifest')) ?? '{}'
  return new Response(manifest, { status: 200, headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' } })
}

/** Handle POST /hooks/sync. GitHub Actions calls it after each commit. */
async function handleSync(request: Request, env: Env): Promise<Response> {
  if (!env.SYNC_TOKEN) {
    // Never fail open. A missing secret means the hook is closed.
    return jsonResponse({ error: 'sync_not_configured' }, 503)
  }

  const header = request.headers.get('authorization') ?? ''
  const prefix = 'Bearer '
  if (!header.startsWith(prefix) || !tokensMatch(header.slice(prefix.length), env.SYNC_TOKEN)) {
    return jsonResponse({ error: 'unauthorized' }, 401)
  }

  // Reject a big body before reading it. Then cap the read itself.
  const declared = Number(request.headers.get('content-length') ?? '0')
  if (declared > MAX_HOOK_BODY) {
    return jsonResponse({ error: 'bad_request', detail: 'body too large' }, 400)
  }
  const raw = await request.text()
  if (raw.length > MAX_HOOK_BODY) {
    return jsonResponse({ error: 'bad_request', detail: 'body too large' }, 400)
  }

  let sha: unknown
  try {
    sha = (JSON.parse(raw) as { sha?: unknown }).sha
  } catch {
    return jsonResponse({ error: 'bad_request', detail: 'body is not JSON' }, 400)
  }

  const isCommit = typeof sha === 'string' && SHA_PATTERN.test(sha)
  const isUnpin = sha === env.DEFAULT_BRANCH || sha === 'main'
  if (!isCommit && !isUnpin) {
    return jsonResponse({ error: 'bad_request', detail: 'sha must be 40 hex chars or a branch name' }, 400)
  }

  const syncedAt = new Date().toISOString()
  // An unpin removes the key. Then readRef falls back to the default branch.
  if (isCommit) {
    await env.STATE.put('sha', sha as string)
  } else {
    await env.STATE.delete('sha')
  }
  await env.STATE.put('synced_at', syncedAt)

  return jsonResponse({ ok: true, sha, synced_at: syncedAt }, 200, { 'cache-control': 'no-store' })
}

/** Handle GET /health.json. It reports the SHA the site serves right now. */
async function handleHealth(env: Env): Promise<Response> {
  const [ref, syncedAt] = await Promise.all([readRef(env), env.STATE.get('synced_at')])
  return jsonResponse(
    {
      ok: true,
      sha: ref,
      synced_at: syncedAt,
      repo: env.REPO,
      site_root: env.SITE_ROOT
    },
    200,
    { 'cache-control': 'no-store', 'access-control-allow-origin': '*' }
  )
}

/** Proxy one static file from raw.githubusercontent.com. */
async function handleStatic(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
  const url = new URL(request.url)

  if (isUnsafePath(url.pathname)) {
    return textResponse('Bad request.', 400)
  }

  const [ref, version] = await Promise.all([readRef(env), env.STATE.get('version')])
  const isPinned = SHA_PATTERN.test(ref)
  const ttl = isPinned ? TTL_PINNED : TTL_BRANCH
  const cacheKey = cacheKeyFor(request, `${ref}:${version ?? ''}`)
  const cache = caches.default

  const hit = await cache.match(cacheKey)
  if (hit) {
    return request.method === 'HEAD' ? new Response(null, { status: hit.status, headers: hit.headers }) : hit
  }

  const filePath = mapPath(url.pathname)

  // 1) KV copy uploaded by the pipeline (works for private repos).
  const stored = await env.STATE.getWithMetadata<{ ct?: string }>(FILE_PREFIX + filePath, { type: 'arrayBuffer' })
  if (stored.value) {
    const headers = new Headers({
      'content-type': stored.metadata?.ct ?? contentTypeFor(filePath),
      'cache-control': `public, max-age=${TTL_KV}`,
      'x-site-source': 'kv'
    })
    if (extensionOf(filePath) === 'json') headers.set('access-control-allow-origin', '*')
    const response = new Response(stored.value, { status: 200, headers })
    ctx.waitUntil(cache.put(cacheKey, response.clone()))
    return request.method === 'HEAD' ? new Response(null, { status: 200, headers }) : response
  }

  // 2) Fallback: raw.githubusercontent.com (public repos only).
  // Strip stray slashes from SITE_ROOT. filePath always starts with "/".
  const siteRoot = env.SITE_ROOT.replace(/^\/+|\/+$/g, '')
  const upstreamUrl = `https://raw.githubusercontent.com/${env.REPO}/${ref}/${siteRoot}${filePath}`

  let upstream: Response
  try {
    upstream = await fetch(upstreamUrl)
  } catch {
    return textResponse('Upstream fetch failed.', 502)
  }

  if (upstream.status === 404) {
    return new Response(NOT_FOUND_HTML, {
      status: 404,
      headers: { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' }
    })
  }
  if (upstream.status !== 200) {
    // 5xx, 429, or anything else from raw. The client should retry, not cache.
    return textResponse(`Upstream returned ${upstream.status}.`, 502)
  }

  // Build clean headers. Nothing is copied from the raw response.
  const headers = new Headers({
    'content-type': contentTypeFor(filePath),
    'cache-control': `public, max-age=${ttl}`,
    'x-site-ref': ref
  })
  if (extensionOf(filePath) === 'json') {
    // /.well-known/nostr.json and similar files need cross-origin reads.
    headers.set('access-control-allow-origin', '*')
  }

  const response = new Response(upstream.body, { status: 200, headers })

  if (request.method === 'HEAD') {
    // Give the whole body to the cache. Do not clone it. A cloned body with one
    // undrained branch makes the runtime buffer the file in memory.
    ctx.waitUntil(cache.put(cacheKey, response))
    return new Response(null, { status: 200, headers })
  }

  ctx.waitUntil(cache.put(cacheKey, response.clone()))
  return response
}

export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    // Check config on every request. The check is a few string compares.
    const missing: string[] = REQUIRED_VARS.filter((key) => typeof env[key] !== 'string' || env[key].length === 0)
    if (typeof env.SITE_ROOT !== 'string') missing.push('SITE_ROOT')
    if (!env.STATE) missing.push('STATE')
    if (missing.length > 0) {
      return textResponse(`Worker is misconfigured. Missing: ${missing.join(', ')}.`, 500)
    }

    const url = new URL(request.url)
    const method = request.method

    if (method === 'POST') {
      if (url.pathname === '/hooks/sync') return handleSync(request, env)
      if (url.pathname === '/hooks/upload') return handleUpload(request, env)
      return jsonResponse({ error: 'method_not_allowed' }, 405, { allow: 'GET, HEAD' })
    }
    if (url.pathname === '/hooks/manifest' && method === 'GET') return handleManifest(request, env)

    if (method !== 'GET' && method !== 'HEAD') {
      return jsonResponse({ error: 'method_not_allowed' }, 405, { allow: 'GET, HEAD, POST' })
    }

    if (url.pathname === '/health.json') return handleHealth(env)

    // Bridgy Fed gives the site a custom Bluesky handle. It reads this path.
    if (url.pathname === '/.well-known/atproto-did') {
      const target = `https://fed.brid.gy/.well-known/atproto-did?protocol=web&id=${encodeURIComponent(env.BRIDGY_WEB_ID)}`
      return new Response(null, {
        status: 302,
        headers: { location: target, 'cache-control': 'no-store' }
      })
    }

    return handleStatic(request, env, ctx)
  }
} satisfies ExportedHandler<Env>
