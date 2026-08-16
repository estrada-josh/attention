// Mastodon helper: register an app, create an account by API, confirm the
// email through AgentMail, and post. Zero dependencies (Node >= 20).
//
// WHY: mastodon.social has open registration (verified 2026-08-16:
// registrations.enabled=true, approval_required=false). The API path is
// POST /api/v1/apps -> POST /oauth/token (client_credentials) ->
// POST /api/v1/accounts -> confirmation email -> click link.
//
// CLI:
//   node tools/mastodon.mjs signup <instance> <username> <email> <display>   (prints JSON with tokens; also writes .env lines to stdout)
//   node tools/mastodon.mjs verify <instance> <token>                        (GET /api/v1/accounts/verify_credentials)
//   node tools/mastodon.mjs post <instance> <token> "<text>"
//   node tools/mastodon.mjs update <instance> <token> --bot --note "<bio>" --name "<display>"
import { randomBytes } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { waitFor, extractLinks } from './agentmail.mjs'

const log = (...a) => console.error(...a)

async function j(url, opts = {}) {
  const res = await fetch(url, opts)
  const text = await res.text()
  let data; try { data = JSON.parse(text) } catch { data = { raw: text } }
  if (!res.ok) throw new Error(`${opts.method || 'GET'} ${url} -> ${res.status}: ${text.slice(0, 500)}`)
  return data
}
const form = (o) => { const f = new URLSearchParams(); for (const [k, v] of Object.entries(o)) if (v !== undefined) f.set(k, String(v)); return f }

export async function createApp(instance, name, website) {
  return j(`https://${instance}/api/v1/apps`, { method: 'POST', body: form({ client_name: name, redirect_uris: 'urn:ietf:wg:oauth:2.0:oob', scopes: 'read write follow push', website }) })
}
export async function appToken(instance, app) {
  return j(`https://${instance}/oauth/token`, { method: 'POST', body: form({ client_id: app.client_id, client_secret: app.client_secret, grant_type: 'client_credentials', scope: 'read write follow push' }) })
}
export async function registerAccount(instance, token, { username, email, password, locale = 'en', reason, date_of_birth }) {
  return j(`https://${instance}/api/v1/accounts`, { method: 'POST', headers: { Authorization: `Bearer ${token}` }, body: form({ username, email, password, agreement: 'true', locale, reason, date_of_birth }) })
}
export async function verify(instance, token) {
  return j(`https://${instance}/api/v1/accounts/verify_credentials`, { headers: { Authorization: `Bearer ${token}` } })
}
export async function post(instance, token, status, extra = {}) {
  return j(`https://${instance}/api/v1/statuses`, { method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Idempotency-Key': randomBytes(12).toString('hex') }, body: form({ status, visibility: 'public', language: 'en', ...extra }) })
}
export async function updateProfile(instance, token, fields) {
  return j(`https://${instance}/api/v1/accounts/update_credentials`, { method: 'PATCH', headers: { Authorization: `Bearer ${token}` }, body: form(fields) })
}

// Follow the Mastodon confirmation link with a GET (Mastodon confirms on GET).
export async function clickConfirmLink(url) {
  const res = await fetch(url, { redirect: 'manual' })
  return { status: res.status, location: res.headers.get('location') }
}

async function main() {
  const [cmd, ...a] = process.argv.slice(2)
  if (cmd === 'signup') {
    const [instance, username, email, display] = a
    const password = randomBytes(18).toString('base64url')
    log('1) create app'); const app = await createApp(instance, display || username, `https://${instance}/@${username}`)
    log('2) app token'); const at = await appToken(instance, app)
    const since = new Date()
    log('3) register account'); const tok = await registerAccount(instance, at.access_token, { username, email, password, date_of_birth: process.env.DOB })
    log('4) wait for confirmation email at', email)
    const hit = await waitFor(email, m => { const l = extractLinks(m).filter(u => u.includes(instance) && /confirm/i.test(u)); return l.length ? { links: l } : null }, 600, since)
    log('5) confirm:', hit.links[0]); const c = await clickConfirmLink(hit.links[0]); log('   ->', c.status, c.location || '')
    log('6) verify'); let me = null; try { me = await verify(instance, tok.access_token) } catch (e) { log('   verify failed (may need a moment):', e.message) }
    console.log(JSON.stringify({ instance, username, email, password, client_id: app.client_id, client_secret: app.client_secret, access_token: tok.access_token, account: me && { id: me.id, acct: me.acct, url: me.url } }, null, 2))
    return
  }
  if (cmd === 'verify') { console.log(JSON.stringify(await verify(a[0], a[1]), null, 2)); return }
  if (cmd === 'post') { const r = await post(a[0], a[1], a[2]); console.log(r.url); return }
  if (cmd === 'update') {
    const [instance, token, ...rest] = a; const f = {}
    for (let i = 0; i < rest.length; i++) { if (rest[i] === '--bot') f.bot = 'true'; else if (rest[i] === '--note') f.note = rest[++i]; else if (rest[i] === '--name') f.display_name = rest[++i]; else if (rest[i] === '--discoverable') f.discoverable = 'true' }
    const r = await updateProfile(instance, token, f); console.log(r.url, 'bot=', r.bot); return
  }
  console.error('usage: signup|verify|post|update'); process.exit(2)
}
if (process.argv[1] === fileURLToPath(import.meta.url)) main().catch(e => { console.error('error:', e.message); process.exit(1) })
