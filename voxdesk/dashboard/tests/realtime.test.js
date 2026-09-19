/**
 * Realtime client: handshake shape, delivery dispatch, dedupe, token
 * rotation on a 1008 policy close, and reconnect discipline — pinned against
 * a FakeWebSocket so the wire frames are asserted literally, the same frames
 * the Go gateway's protocol package documents.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// The client talks to api.js only through these two exports. The harness
// drives a hoisted state object so "login happened" / "session over" are
// flippable mid-test.
const auth = vi.hoisted(() => ({
  token: 'token-a',
  refreshOk: true,
  refreshCalls: 0,
  rotatedToken: 'token-b',
}))

vi.mock('../src/lib/api', () => ({
  getAccessToken: () => auth.token,
  refreshAccessToken: async () => {
    auth.refreshCalls += 1
    if (!auth.refreshOk) return false
    auth.token = auth.rotatedToken
    return true
  },
}))

import { RealtimeClient, ROOM_CALLS, defaultGatewayUrl, roomCall } from '../src/lib/realtime'

class FakeWebSocket {
  static instances = []
  constructor(url) {
    this.url = url
    this.sent = []
    this.readyState = 0 // CONNECTING
    this.onopen = null
    this.onmessage = null
    this.onclose = null
    this.onerror = null
    FakeWebSocket.instances.push(this)
  }
  send(data) {
    this.sent.push(JSON.parse(data))
  }
  close(code = 1000, reason = '') {
    this.readyState = 3
    this.onclose?.({ code, reason })
  }
  // ---- test drivers (what the real gateway would do) ----
  open() {
    this.readyState = 1
    this.onopen?.()
  }
  receive(frame) {
    this.onmessage?.({ data: JSON.stringify(frame) })
  }
  drop(code = 1006, reason = '') {
    this.readyState = 3
    this.onclose?.({ code, reason })
  }
}

const FAST = { initialMs: 100, maxMs: 400, jitter: () => 0 }

function makeClient(overrides = {}) {
  const events = []
  const states = []
  const client = new RealtimeClient({
    url: 'ws://test/realtime/ws',
    socketFactory: (url) => new FakeWebSocket(url),
    backoff: FAST,
    onEvent: (event) => events.push(event),
    onState: (state) => states.push(state),
    ...overrides,
  })
  return { client, events, states }
}

/** Flush microtasks: the auth-refresh paths in _open/_handleClose are async.
 * Deliberately microtask-based — fake timers would eat a setTimeout(0). */
async function flush() {
  for (let i = 0; i < 8; i += 1) await Promise.resolve()
}

beforeEach(() => {
  FakeWebSocket.instances = []
  auth.token = 'token-a'
  auth.refreshOk = true
  auth.refreshCalls = 0
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('URL mapping', () => {
  it('maps the page scheme to the single public gateway path', () => {
    expect(defaultGatewayUrl({ protocol: 'https:', host: 'app.example.com' }))
      .toBe('wss://app.example.com/realtime/ws')
    expect(defaultGatewayUrl({ protocol: 'http:', host: 'localhost:5173' }))
      .toBe('ws://localhost:5173/realtime/ws')
  })
})

describe('handshake and subscriptions', () => {
  it('sends hello with the access token as the first frame', () => {
    const { client } = makeClient()
    client.connect()
    const socket = FakeWebSocket.instances[0]
    expect(socket.url).toBe('ws://test/realtime/ws')
    socket.open()
    expect(socket.sent).toEqual([{ type: 'hello', token: 'token-a' }])
  })

  it('flushes remembered subscriptions only after the ready frame', () => {
    const { client } = makeClient()
    client.subscribe(ROOM_CALLS)
    client.connect()
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.receive({ type: 'welcome', auth_required: true })
    // Before ready, no subscribe frame may leak out (the gateway would treat
    // it as unauthenticated traffic).
    expect(socket.sent).toEqual([{ type: 'hello', token: 'token-a' }])
    socket.receive({ type: 'ready', session_id: 's1', tenant_id: 't1', role: 'owner' })
    expect(socket.sent[1]).toEqual({ type: 'subscribe', room: 'calls' })
  })

  it('subscribes immediately when the connection is already ready', () => {
    const { client } = makeClient()
    client.connect()
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.receive({ type: 'ready', session_id: 's1', tenant_id: 't1', role: 'owner' })
    client.subscribe(roomCall('abc-123'))
    expect(socket.sent.at(-1)).toEqual({ type: 'subscribe', room: 'call:abc-123' })
    client.unsubscribe(roomCall('abc-123'))
    expect(socket.sent.at(-1)).toEqual({ type: 'unsubscribe', room: 'call:abc-123' })
  })
})

describe('deliveries', () => {
  function readyClient() {
    const harness = makeClient()
    harness.client.subscribe(ROOM_CALLS)
    harness.client.connect()
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.receive({ type: 'ready', session_id: 's1', tenant_id: 't1', role: 'owner' })
    return { ...harness, socket }
  }

  it('hands parsed delivery frames to onEvent', () => {
    const { client, events, socket } = readyClient()
    socket.receive({
      type: 'delivery', room: 'calls', kind: 'call.updated',
      payload: { call_id: 'c-1', status: 'completed' },
      event_id: 'e-111', sent_at: '2026-09-16T10:00:00Z',
    })
    expect(events).toEqual([{
      room: 'calls',
      kind: 'call.updated',
      payload: { call_id: 'c-1', status: 'completed' },
      eventId: 'e-111',
      sentAt: '2026-09-16T10:00:00Z',
    }])
    client.close()
  })

  it('drops exact duplicate event ids on the same connection', () => {
    const { client, events, socket } = readyClient()
    const frame = {
      type: 'delivery', room: 'calls', kind: 'call.updated',
      payload: { call_id: 'c-1' }, event_id: 'e-same',
    }
    socket.receive(frame)
    socket.receive(frame)
    expect(events).toHaveLength(1)
    client.close()
  })

  it('delivers the same logical event per room without collapsing it', () => {
    const { client, events, socket } = readyClient()
    // List page and detail page both subscribed: the gateway gives each room
    // its own event id ON PURPOSE (per-room replay windows), so the client
    // must NOT dedupe across rooms.
    socket.receive({
      type: 'delivery', room: 'calls', kind: 'call.updated',
      payload: { call_id: 'c-1', status: 'completed' }, event_id: 'e-list',
    })
    socket.receive({
      type: 'delivery', room: 'call:c-1', kind: 'call.updated',
      payload: { call_id: 'c-1', status: 'completed' }, event_id: 'e-detail',
    })
    expect(events.map((event) => event.room)).toEqual(['calls', 'call:c-1'])
    client.close()
  })

  it('ignores malformed frames instead of dying', () => {
    const { client, events, socket } = readyClient()
    socket.onmessage({ data: 'this is not json{' })
    socket.receive({ type: 'something-new', future: true })
    expect(events).toEqual([])
    expect(client.state).toBe('ready')
    client.close()
  })
})

describe('token rotation on policy close', () => {
  async function readySocket() {
    const harness = makeClient()
    harness.client.subscribe(ROOM_CALLS)
    harness.client.connect()
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.receive({ type: 'ready', session_id: 's1', tenant_id: 't1', role: 'owner' })
    return { ...harness, socket }
  }

  it('1008 token_expired refreshes once and reconnects with the new token', async () => {
    const { client, socket, states } = await readySocket()
    socket.drop(1008, 'token_expired')
    await flush()

    expect(auth.refreshCalls).toBe(1)
    expect(FakeWebSocket.instances).toHaveLength(2)
    const next = FakeWebSocket.instances[1]
    next.open()
    expect(next.sent[0]).toEqual({ type: 'hello', token: 'token-b' })
    // Subscriptions are replayed on the new connection after ready.
    next.receive({ type: 'ready', session_id: 's2', tenant_id: 't1', role: 'owner' })
    expect(next.sent[1]).toEqual({ type: 'subscribe', room: 'calls' })
    expect(states).toContain('ready')
    client.close()
  })

  it('a failed rotation stops instead of hammering', async () => {
    auth.refreshOk = false
    const { client, socket, states } = await readySocket()
    socket.drop(1008, 'authentication failed')
    await flush()

    expect(auth.refreshCalls).toBe(1)
    expect(FakeWebSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(60_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(client.state).toBe('unauthorized')
    expect(states).toContain('unauthorized')
    client.close()
  })
})

describe('reconnect discipline', () => {
  it('an ordinary network drop retries on the configured backoff', async () => {
    const { client, states } = makeClient()
    client.connect()
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.receive({ type: 'ready', session_id: 's1', tenant_id: 't1', role: 'owner' })

    socket.drop(1006, '')
    expect(client.state).toBe('reconnecting')
    expect(FakeWebSocket.instances).toHaveLength(1)

    vi.advanceTimersByTime(99)
    expect(FakeWebSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1)
    expect(FakeWebSocket.instances).toHaveLength(2)
    expect(states).toEqual(
      expect.arrayContaining(['connecting', 'ready', 'reconnecting'])
    )
    client.close()
  })

  it('close() is final: no timer can resurrect the socket', async () => {
    const { client } = makeClient()
    client.connect()
    const socket = FakeWebSocket.instances[0]
    socket.open()
    socket.drop(1006, '')
    client.close()
    vi.advanceTimersByTime(300_000)
    expect(FakeWebSocket.instances).toHaveLength(1)
    expect(client.state).toBe('closed')
  })

  it('with no token yet it retries quietly without opening a refused socket', async () => {
    auth.token = null
    auth.refreshOk = false
    const { client } = makeClient()
    client.connect()
    await flush()
    expect(auth.refreshCalls).toBe(1)
    expect(FakeWebSocket.instances).toHaveLength(0)
    // Login completes on the next retry tick: a socket appears and hello
    // carries whatever token is current then.
    auth.token = 'token-login'
    vi.advanceTimersByTime(100)
    await flush()
    expect(FakeWebSocket.instances).toHaveLength(1)
    const socket = FakeWebSocket.instances[0]
    socket.open()
    expect(socket.sent[0]).toEqual({ type: 'hello', token: 'token-login' })
    client.close()
  })
})
