// AgentMail helper. Zero dependencies (Node >= 20).
//
// WHY: the pipeline signs up for services with an AgentMail inbox. This
// module creates the inbox, lists messages, reads one message, and waits
// for a verification link or code. Progress goes to stderr. Data goes to
// stdout.
//
// CLI:
//   node tools/agentmail.mjs create <username> [display]   -> creates <username>@agentmail.to
//   node tools/agentmail.mjs list <inbox> [limit]
//   node tools/agentmail.mjs read <inbox> <message_id>
//   node tools/agentmail.mjs wait-link <inbox> <host-substring> [seconds]
//   node tools/agentmail.mjs wait-code <inbox> [seconds]
//   node tools/agentmail.mjs send <inbox> <to> <subject> <text>
//
// API shapes (verified live 2026-08-09 in the assay repo; re-verified today):
//   GET  /v0/inboxes                       -> {count, inboxes:[{inbox_id,...}]}
//   POST /v0/inboxes {username, display_name}
//   GET  /v0/inboxes/{id}/messages?limit=N -> {count, messages:[{message_id, subject, preview, from, timestamp,...}]}
//   GET  /v0/inboxes/{id}/messages/{mid}   -> + text / html / extracted_text
//   POST /v0/inboxes/{id}/messages/send {to:[..], subject, text}
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

const API = 'https://api.agentmail.to/v0'
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

export function loadEnv(file = path.join(ROOT, '.env')) {
  const env = {}
  let text = ''
  try { text = readFileSync(file, 'utf8') } catch { return env }
  for (let line of text.split('\n')) {
    line = line.trim()
    if (!line || line.startsWith('#') || !line.includes('=')) continue
    if (line.startsWith('export ')) line = line.slice(7).trim()
    const i = line.indexOf('=')
    let k = line.slice(0, i).trim(), v = line.slice(i + 1).trim()
    if (v.length >= 2 && v[0] === v.at(-1) && `'"`.includes(v[0])) v = v.slice(1, -1)
    env[k] = v
  }
  return env
}

export function apiKey() {
  const key = process.env.AGENTMAIL_API_KEY || loadEnv().AGENTMAIL_API_KEY
  if (!key) throw new Error('AGENTMAIL_API_KEY is not set (env or repo .env)')
  return key
}

async function call(method, p, body, params) {
  const url = new URL(API + p)
  if (params) for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v)
  const res = await fetch(url, {
    method,
    headers: { Authorization: `Bearer ${apiKey()}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  })
  const text = await res.text()
  let data
  try { data = JSON.parse(text) } catch { data = { raw: text } }
  if (!res.ok) throw new Error(`AgentMail ${method} ${p} -> ${res.status}: ${text.slice(0, 400)}`)
  return data
}

const enc = (s) => encodeURIComponent(s)
export const listInboxes = () => call('GET', '/inboxes')
export const createInbox = (username, display_name) => call('POST', '/inboxes', { username, display_name })
export const listMessages = (inbox, limit = 20) => call('GET', `/inboxes/${enc(inbox)}/messages`, null, { limit })
export const getMessage = (inbox, mid) => call('GET', `/inboxes/${enc(inbox)}/messages/${enc(mid)}`)
export const sendMessage = (inbox, to, subject, text, html) =>
  call('POST', `/inboxes/${enc(inbox)}/messages/send`, { to: Array.isArray(to) ? to : [to], subject, text, html })

export function bodyText(m) {
  return m.text || m.extracted_text || (m.html ? m.html.replace(/<[^>]+>/g, ' ') : '') || m.preview || ''
}

export function extractLinks(m) {
  const out = new Set()
  const html = m.html || ''
  for (const x of html.matchAll(/href=["']([^"']+)["']/gi)) out.add(x[1].replace(/&amp;/g, '&'))
  for (const x of (m.text || m.extracted_text || '').matchAll(/https?:\/\/[^\s<>"')\]]+/g)) out.add(x[0])
  return [...out]
}

export function extractCode(subject, body) {
  const texts = [subject || '', body || '']
  const word = '(?:code|codes|passcode|otp|pin)'
  const res = [
    new RegExp(`\\b${word}\\s*(?:is[:\\s]\\s*|:\\s*)([A-Za-z0-9]{4,8})\\b`, 'i'),
    new RegExp(`\\b([A-Za-z0-9]{4,8})\\b\\s+is\\s+your\\b[^.!\\n]{0,40}?\\b${word}\\b`, 'i'),
    new RegExp(`\\b(\\d{4,8})\\b`),
  ]
  for (const re of res) for (const t of texts) { const m = t.match(re); if (m) return m[1] }
  return null
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms))
const log = (...a) => console.error(...a)

// Wait for a NEW message (after `since`) whose links contain hostSub. Returns {message, links}.
export async function waitFor(inbox, pred, seconds = 300, since = new Date()) {
  const deadline = Date.now() + seconds * 1000
  const seen = new Set()
  log(`waiting on ${inbox} (max ${seconds}s) ...`)
  while (Date.now() < deadline) {
    let list = []
    try { list = (await listMessages(inbox, 20)).messages || [] } catch (e) { log('poll failed:', e.message) }
    for (const item of list) {
      if (seen.has(item.message_id)) continue
      seen.add(item.message_id)
      if (new Date(item.timestamp) <= since) continue
      log(`new message from ${item.from}: ${item.subject}`)
      let full = item
      try { full = await getMessage(inbox, item.message_id) } catch (e) { log('read failed:', e.message) }
      const hit = pred(full)
      if (hit) return hit
    }
    await sleep(8000)
  }
  throw new Error('timeout waiting for message')
}

async function main() {
  const [cmd, ...args] = process.argv.slice(2)
  if (cmd === 'create') { console.log(JSON.stringify(await createInbox(args[0], args[1] || args[0]), null, 2)); return }
  if (cmd === 'inboxes') { console.log(JSON.stringify(await listInboxes(), null, 2)); return }
  if (cmd === 'list') {
    const d = await listMessages(args[0], Number(args[1] || 20))
    for (const m of d.messages || []) console.log(`${m.timestamp}  ${m.from}  ${m.subject}  ${m.message_id}`)
    if (!d.messages?.length) console.log('(no messages)')
    return
  }
  if (cmd === 'read') { const m = await getMessage(args[0], args[1]); console.log(bodyText(m)); console.log('\nLINKS:\n' + extractLinks(m).join('\n')); return }
  if (cmd === 'wait-link') {
    const since = new Date(Date.now() - 60_000)
    const r = await waitFor(args[0], m => { const l = extractLinks(m).filter(u => u.includes(args[1])); return l.length ? { links: l, subject: m.subject } : null }, Number(args[2] || 300), since)
    console.log(r.links.join('\n')); return
  }
  if (cmd === 'wait-code') {
    const since = new Date(Date.now() - 60_000)
    const r = await waitFor(args[0], m => { const c = extractCode(m.subject, bodyText(m)); return c ? { code: c } : null }, Number(args[1] || 300), since)
    console.log(r.code); return
  }
  if (cmd === 'send') { console.log(JSON.stringify(await sendMessage(args[0], args[1], args[2], args[3]), null, 2)); return }
  console.error('usage: create|inboxes|list|read|wait-link|wait-code|send'); process.exit(2)
}
if (process.argv[1] === fileURLToPath(import.meta.url)) main().catch(e => { console.error('error:', e.message); process.exit(1) })
