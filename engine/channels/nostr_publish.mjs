#!/usr/bin/env node
// Nostr publisher for the attention engine.
//
// The script publishes profile metadata, short notes, and long articles to
// Nostr relays. It also generates keys and verifies that a relay stored an
// event.
//
// The script reads the private key from the environment variable NOSTR_NSEC.
// The script never prints the private key.
//
// Usage:
//   node engine/channels/nostr_publish.mjs keygen
//   node engine/channels/nostr_publish.mjs profile --name "Bot" --about "..."
//   node engine/channels/nostr_publish.mjs note --content "hello"
//   node engine/channels/nostr_publish.mjs article --title T --summary S \
//     --content-file post.md --identifier slug
//   node engine/channels/nostr_publish.mjs verify --id <event-id>
//
// Exit codes:
//   0 = at least one relay accepted the event, or the command succeeded.
//   1 = every relay failed, or the verify command found nothing.
//   2 = usage error.

import { generateSecretKey, getPublicKey, finalizeEvent } from 'nostr-tools/pure'
import { SimplePool, useWebSocketImplementation } from 'nostr-tools/pool'
import * as nip19 from 'nostr-tools/nip19'
import { hexToBytes } from 'nostr-tools/utils'

// nostr-tools needs a WebSocket class. Node 22 and later supply a global one.
// Older Node versions need the `ws` package.
if (typeof globalThis.WebSocket === 'function') {
  useWebSocketImplementation(globalThis.WebSocket)
} else {
  const { default: NodeWebSocket } = await import('ws')
  useWebSocketImplementation(NodeWebSocket)
}

const DEFAULT_RELAYS = [
  'wss://relay.damus.io',
  'wss://nos.lol',
  'wss://relay.primal.net',
  'wss://purplepag.es',
]

// Each relay gets 10 seconds. After that the script marks the relay as failed.
const RELAY_TIMEOUT_MS = 10_000

const EXIT_OK = 0
const EXIT_FAILED = 1
const EXIT_USAGE = 2

// ---------------------------------------------------------------------------
// Command line parsing
// ---------------------------------------------------------------------------

/**
 * Parse the command line into a subcommand and a flag map.
 * Each flag can repeat. The parser keeps every value in an array.
 */
function parseArgs(argv) {
  const command = argv[0]
  const flags = new Map()

  for (let i = 1; i < argv.length; i++) {
    const token = argv[i]
    if (!token.startsWith('--')) {
      throw new UsageError(`unexpected argument "${token}"`)
    }
    const name = token.slice(2)
    const value = argv[i + 1]
    if (value === undefined || (value.startsWith('--') && value !== '-')) {
      throw new UsageError(`flag "--${name}" needs a value`)
    }
    i++
    if (!flags.has(name)) flags.set(name, [])
    flags.get(name).push(value)
  }

  return { command, flags }
}

class UsageError extends Error {}

/** Return the single value of a flag, or undefined. */
function flag(flags, name) {
  const values = flags.get(name)
  return values ? values[values.length - 1] : undefined
}

/** Return the single value of a flag. Throw a usage error when it is missing. */
function requireFlag(flags, name) {
  const value = flag(flags, name)
  if (value === undefined || value === '') {
    throw new UsageError(`flag "--${name}" is required`)
  }
  return value
}

/** Return every value of a repeated flag. */
function flagList(flags, name) {
  return flags.get(name) ?? []
}

// ---------------------------------------------------------------------------
// Environment
// ---------------------------------------------------------------------------

/**
 * Read the private key from NOSTR_NSEC.
 * The value is an nsec bech32 string or a 64 character hex string.
 * The function returns the raw 32 byte key. It never logs the key.
 */
function loadSecretKey() {
  const raw = (process.env.NOSTR_NSEC ?? '').trim()
  if (raw === '') {
    throw new UsageError('environment variable NOSTR_NSEC is not set')
  }

  if (raw.startsWith('nsec1')) {
    let decoded
    try {
      decoded = nip19.decode(raw)
    } catch {
      throw new UsageError('NOSTR_NSEC is not a valid nsec string')
    }
    if (decoded.type !== 'nsec') {
      throw new UsageError(`NOSTR_NSEC holds a "${decoded.type}" value, not an nsec`)
    }
    return decoded.data
  }

  if (/^[0-9a-fA-F]{64}$/.test(raw)) {
    return hexToBytes(raw.toLowerCase())
  }

  throw new UsageError('NOSTR_NSEC must be an nsec string or 64 hex characters')
}

/** Read the relay list from NOSTR_RELAYS, or fall back to the defaults. */
function loadRelays() {
  const raw = (process.env.NOSTR_RELAYS ?? '').trim()
  const list = raw === ''
    ? DEFAULT_RELAYS
    : raw.split(',').map(entry => entry.trim()).filter(entry => entry !== '')

  // Duplicate relay URLs make the pool reject the extra copies.
  const unique = [...new Set(list)]
  if (unique.length === 0) {
    throw new UsageError('NOSTR_RELAYS is empty')
  }
  return unique
}

/** Read the whole standard input as UTF-8 text. */
async function readStdin() {
  const chunks = []
  for await (const chunk of process.stdin) chunks.push(chunk)
  return Buffer.concat(chunks).toString('utf8')
}

// ---------------------------------------------------------------------------
// Publishing
// ---------------------------------------------------------------------------

/** Turn any thrown value into a short message. */
function errorText(value) {
  if (value instanceof Error) return value.message
  if (typeof value === 'string') return value
  return String(value)
}

/** Reject the promise when it takes longer than `ms` milliseconds. */
function withTimeout(promise, ms) {
  let timer
  const limit = new Promise((_resolve, reject) => {
    timer = setTimeout(() => reject(new Error(`timeout after ${ms} ms`)), ms)
  })
  return Promise.race([Promise.resolve(promise), limit]).finally(() => clearTimeout(timer))
}

/**
 * Sign the event template and send it to every relay.
 * The function waits for all relays and returns one report.
 */
async function publishEvent(pool, relays, secretKey, template) {
  const event = finalizeEvent(template, secretKey)
  const pending = pool.publish(relays, event, { maxWait: RELAY_TIMEOUT_MS })

  const results = await Promise.all(
    relays.map((relay, index) =>
      withTimeout(pending[index], RELAY_TIMEOUT_MS)
        .then(() => ({ relay, ok: true }))
        .catch(error => ({ relay, ok: false, error: errorText(error) })),
    ),
  )

  const ok = results.filter(result => result.ok).map(result => result.relay)
  const failed = results
    .filter(result => !result.ok)
    .map(result => ({ relay: result.relay, error: result.error }))

  return { id: event.id, ok, failed }
}

/** Print the publish report and pick the exit code. */
function reportPublish(report) {
  console.log(JSON.stringify(report, null, 2))
  return report.ok.length > 0 ? EXIT_OK : EXIT_FAILED
}

// ---------------------------------------------------------------------------
// Tag helpers
// ---------------------------------------------------------------------------

/** Normalise a hashtag. Remove the leading "#" and lowercase the rest. */
function normaliseTopic(value) {
  return value.trim().replace(/^#+/, '').toLowerCase()
}

// ---------------------------------------------------------------------------
// Subcommands
// ---------------------------------------------------------------------------

/** Generate a new key pair and print it. */
function commandKeygen() {
  const secretKey = generateSecretKey()
  const pubkeyHex = getPublicKey(secretKey)
  console.log(JSON.stringify({
    nsec: nip19.nsecEncode(secretKey),
    npub: nip19.npubEncode(pubkeyHex),
    pubkey_hex: pubkeyHex,
  }, null, 2))
  return EXIT_OK
}

/** Publish the kind 0 profile metadata event. */
async function commandProfile(pool, relays, secretKey, flags) {
  const metadata = { bot: true }
  const fields = [
    ['name', 'name'],
    ['about', 'about'],
    ['picture', 'picture'],
    ['website', 'website'],
    ['nip05', 'nip05'],
    ['banner', 'banner'],
  ]
  for (const [flagName, field] of fields) {
    const value = flag(flags, flagName)
    if (value !== undefined) metadata[field] = value
  }

  if (Object.keys(metadata).length === 1) {
    throw new UsageError('profile needs at least one field, for example --name')
  }

  return publishEvent(pool, relays, secretKey, {
    kind: 0,
    created_at: Math.floor(Date.now() / 1000),
    tags: [],
    content: JSON.stringify(metadata),
  })
}

/** Publish a kind 1 short note. */
async function commandNote(pool, relays, secretKey, flags) {
  let content = requireFlag(flags, 'content')
  if (content === '-') {
    content = (await readStdin()).trim()
  }
  if (content === '') {
    throw new UsageError('note content is empty')
  }

  const tags = []

  const image = flag(flags, 'image')
  if (image !== undefined) {
    // NIP-92 attaches the image metadata to the URL inside the content.
    content += `\n\n${image}`
    tags.push(['imeta', `url ${image}`, 'm image/png'])
    tags.push(['r', image])
  }

  const url = flag(flags, 'url')
  if (url !== undefined) {
    content += `\n\n${url}`
    tags.push(['r', url])
  }

  for (const topic of flagList(flags, 'tag')) {
    const normalised = normaliseTopic(topic)
    if (normalised !== '') tags.push(['t', normalised])
  }

  return publishEvent(pool, relays, secretKey, {
    kind: 1,
    created_at: Math.floor(Date.now() / 1000),
    tags,
    content,
  })
}

/** Publish a kind 30023 long article (NIP-23). */
async function commandArticle(pool, relays, secretKey, flags) {
  const title = requireFlag(flags, 'title')
  const summary = requireFlag(flags, 'summary')
  const identifier = requireFlag(flags, 'identifier')
  const contentFile = requireFlag(flags, 'content-file')

  const { readFile } = await import('node:fs/promises')
  let content
  try {
    content = await readFile(contentFile, 'utf8')
  } catch (error) {
    throw new UsageError(`cannot read --content-file "${contentFile}": ${errorText(error)}`)
  }
  if (content.trim() === '') {
    throw new UsageError(`--content-file "${contentFile}" is empty`)
  }

  const publishedAt = Math.floor(Date.now() / 1000)
  const tags = [
    ['d', identifier],
    ['title', title],
    ['summary', summary],
    ['published_at', String(publishedAt)],
  ]

  const image = flag(flags, 'image')
  if (image !== undefined) tags.push(['image', image])

  const url = flag(flags, 'url')
  if (url !== undefined) tags.push(['r', url])

  return publishEvent(pool, relays, secretKey, {
    kind: 30023,
    created_at: publishedAt,
    tags,
    content,
  })
}

/** Ask the first relay for one event id. */
async function commandVerify(pool, relays, flags) {
  const id = requireFlag(flags, 'id')
  if (!/^[0-9a-f]{64}$/i.test(id)) {
    throw new UsageError('--id must be a 64 character hex event id')
  }

  const relay = relays[0]
  let event = null
  try {
    event = await withTimeout(
      pool.get([relay], { ids: [id.toLowerCase()] }, { maxWait: RELAY_TIMEOUT_MS }),
      RELAY_TIMEOUT_MS,
    )
  } catch (error) {
    console.log(JSON.stringify({ found: false, relay, error: errorText(error) }, null, 2))
    return EXIT_FAILED
  }

  console.log(JSON.stringify({ found: event !== null, relay }, null, 2))
  return event !== null ? EXIT_OK : EXIT_FAILED
}

// ---------------------------------------------------------------------------
// Entry point
// ---------------------------------------------------------------------------

const USAGE = `nostr_publish.mjs <command> [flags]

Commands:
  keygen
  profile --name N [--about A] [--picture URL] [--website URL] [--nip05 ID] [--banner URL]
  note    --content TEXT|- [--image URL] [--url URL] [--tag T ...]
  article --title T --summary S --content-file PATH --identifier SLUG [--image URL] [--url URL]
  verify  --id EVENT_ID

Environment:
  NOSTR_NSEC    private key as nsec or 64 hex characters (required to publish)
  NOSTR_RELAYS  comma separated relay list (optional)`

async function main() {
  const { command, flags } = parseArgs(process.argv.slice(2))

  if (command === undefined || command === 'help' || command === '--help') {
    console.error(USAGE)
    return EXIT_USAGE
  }

  if (command === 'keygen') {
    return commandKeygen()
  }

  const relays = loadRelays()
  const pool = new SimplePool()

  try {
    if (command === 'verify') {
      return await commandVerify(pool, relays, flags)
    }

    const secretKey = loadSecretKey()

    if (command === 'profile') return reportPublish(await commandProfile(pool, relays, secretKey, flags))
    if (command === 'note') return reportPublish(await commandNote(pool, relays, secretKey, flags))
    if (command === 'article') return reportPublish(await commandArticle(pool, relays, secretKey, flags))

    throw new UsageError(`unknown command "${command}"`)
  } finally {
    // Close every socket. Lingering sockets keep the process alive.
    try {
      pool.close(relays)
      pool.destroy()
    } catch {
      // The pool is already gone. Ignore the error.
    }
  }
}

let exitCode = EXIT_FAILED
try {
  exitCode = await main()
} catch (error) {
  if (error instanceof UsageError) {
    console.error(JSON.stringify({ error: 'usage', detail: error.message }))
    exitCode = EXIT_USAGE
  } else {
    console.error(JSON.stringify({ error: 'failed', detail: errorText(error) }))
    exitCode = EXIT_FAILED
  }
}

// Exit now. Open relay sockets would otherwise hold the event loop.
process.exit(exitCode)
