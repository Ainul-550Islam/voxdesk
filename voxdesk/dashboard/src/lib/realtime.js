/**
 * Realtime client: the dashboard's WebSocket edge.
 *
 * One durable connection to the gateway carries every live fact the UI needs
 * (calls appearing, statuses changing, transfers resolving). The model is
 * NOTICE, not data: a delivery says "call X changed to state Y" with
 * near-contentless fields, and the detail views re-read through the
 * authenticated REST API if they need more. That mirrors the server side,
 * where the publish path deliberately excludes phone numbers and transcripts.
 *
 * **Auth.** Browsers cannot set headers on a WebSocket, so the JWT goes in
 * the hello frame — which is exactly what the gateway's `reader.go` expects
 * before anything else. The token is obtained from api.js (`getAccessToken`),
 * never from storage; when the gateway closes with 1008 (reason
 * `token_expired` or `authentication failed`) the client rotates the token
 * once via `refreshAccessToken()` and reconnects immediately. A rotation that
 * also fails means the session is over: the client stops and waits for an
 * explicit `connect()` (e.g. after the next login) rather than hammering.
 *
 * **Reconnect.** Any other close — network flap, gateway restart, proxy
 * timeout — retries on exponential backoff with jitter. Subscriptions are
 * remembered and re-sent after every successful `ready`, so a page that
 * subscribed once never has to know a reconnect happened.
 */

import { getAccessToken, refreshAccessToken } from './api'

/** Closed room vocabulary, mirroring app/realtime/events.py and the gateway. */
export const ROOM_CALLS = 'calls'
export const roomCall = (callId) => `call:${callId}`

/**
 * Where the socket lives. Same origin as the page — Caddy routes exactly one
 * public path (/realtime/ws → gateway /ws), so there is no configuration to
 * keep in sync and nothing cross-origin to leak the token to.
 */
export function defaultGatewayUrl(loc = window.location) {
  const scheme = loc.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${loc.host}/realtime/ws`
}

const CLOSE_POLICY = 1008 // gateway: auth failed / timed out / room refused

export class RealtimeClient {
  /**
   * @param {object} opts
   * @param {(event: {room, kind, payload, eventId, sentAt}) => void} opts.onEvent
   * @param {(state: string) => void} [opts.onState]
   *        'connecting' | 'ready' | 'reconnecting' | 'unauthorized' | 'closed'
   * @param {(url: string) => WebSocket} [opts.socketFactory]  test seam
   * @param {{initialMs?: number, maxMs?: number, jitter?: () => number}} [opts.backoff]
   *        jitter() returns [0,1); effective delay is delay * (1 - jitter*0.5)
   */
  constructor({
    url = defaultGatewayUrl(),
    onEvent,
    onState = () => {},
    socketFactory = (u) => new WebSocket(u),
    backoff = {},
  } = {}) {
    if (typeof onEvent !== 'function') {
      throw new TypeError('RealtimeClient requires an onEvent handler')
    }
    this._url = url
    this._onEvent = onEvent
    this._onState = onState
    this._socketFactory = socketFactory
    this._backoff = {
      initialMs: backoff.initialMs ?? 1000,
      maxMs: backoff.maxMs ?? 30000,
      jitter: backoff.jitter ?? (() => Math.random()),
    }

    this._rooms = new Set()
    this._socket = null
    this._ready = false
    this._closedByUs = false
    this._timer = null
    this._attempts = 0
    this.state = 'closed'
    // Deliveries issued before hello completes are deduped per connection by
    // the gateway; across reconnects an id may reappear (we asked again), so
    // the dedupe ring resets with each socket.
    this._seen = new Set()
  }

  /** Begin (or schedule) the connection. Idempotent. */
  connect() {
    if (this._closedByUs === false && (this._socket || this._timer)) return
    this._closedByUs = false
    void this._open()
  }

  /** Permanent shutdown: cancel timers, close the socket, keep no state. */
  close() {
    this._closedByUs = true
    if (this._timer) {
      clearTimeout(this._timer)
      this._timer = null
    }
    const socket = this._socket
    this._socket = null
    this._ready = false
    if (socket && socket.readyState <= 1) socket.close(1000)
    this._setState('closed')
  }

  /**
   * Watch a room. Remembered across reconnects; sent immediately when the
   * connection is already ready.
   */
  subscribe(room) {
    this._rooms.add(room)
    if (this._ready) this._send({ type: 'subscribe', room })
  }

  unsubscribe(room) {
    this._rooms.delete(room)
    if (this._ready) this._send({ type: 'unsubscribe', room })
  }

  get roomList() {
    return [...this._rooms]
  }

  // ------------------------------------------------------------ internals ---

  _setState(state) {
    if (this.state !== state) {
      this.state = state
      this._onState(state)
    }
  }

  async _open() {
    if (this._closedByUs) return
    this._setState(this._attempts === 0 ? 'connecting' : 'reconnecting')

    // No token yet (page just loaded, login pending): don't open a socket we
    // already know will be refused — retry the whole attempt on backoff.
    if (!getAccessToken()) {
      const renewed = await refreshAccessToken()
      if (!renewed) {
        // Not signed in is not a server problem, and the user is about to
        // log in through the normal page flow; retry quietly, slowly.
        this._scheduleReconnect()
        return
      }
    }

    let socket
    try {
      socket = this._socketFactory(this._url)
    } catch {
      this._scheduleReconnect()
      return
    }
    this._socket = socket
    this._seen = new Set()

    socket.onopen = () => {
      this._send({ type: 'hello', token: getAccessToken() ?? '' })
    }
    socket.onmessage = (message) => this._handleFrame(message)
    socket.onerror = () => {
      // onclose always follows; handling errors there keeps one code path.
    }
    socket.onclose = (event) => this._handleClose(event)
  }

  _handleFrame(message) {
    let frame
    try {
      frame = JSON.parse(message.data)
    } catch {
      return // a non-JSON frame is not part of this protocol; ignore
    }
    switch (frame.type) {
      case 'welcome':
        // Informational (auth timeout, heartbeat cadence). Nothing to do —
        // hello is sent on open and timeouts are enforced server-side.
        break
      case 'ready':
        this._ready = true
        this._attempts = 0
        this._setState('ready')
        for (const room of this._rooms) this._send({ type: 'subscribe', room })
        break
      case 'delivery':
        if (frame.event_id) {
          if (this._seen.has(frame.event_id)) return
          this._seen.add(frame.event_id)
          if (this._seen.size > 1024) {
            // Bound the ring: a wallboard runs for weeks on one connection.
            this._seen = new Set([...this._seen].slice(-512))
          }
        }
        this._onEvent({
          room: frame.room,
          kind: frame.kind,
          payload: frame.payload ?? {},
          eventId: frame.event_id ?? null,
          sentAt: frame.sent_at ?? null,
        })
        break
      case 'error':
        // 'auth_failed' etc. are always followed by a close frame (that is
        // the gateway's whole close-flow discipline), so the close handler
        // remains the single decision point.
        break
      case 'pong':
      case 'subscribed':
      case 'unsubscribed':
        break
      default:
        break // forward-compatible: unknown frames are ignorable
    }
  }

  async _handleClose(event) {
    this._socket = null
    this._ready = false
    if (this._closedByUs) {
      this._setState('closed')
      return
    }
    const authFailure =
      event.code === CLOSE_POLICY &&
      (event.reason === 'token_expired' || event.reason === 'authentication failed')
    if (authFailure) {
      const renewed = await refreshAccessToken()
      if (renewed) {
        // Fresh token: retry NOW, not after backoff — the moment a
        // wallboard's token expires mid-shift is the moment it most needs
        // to come straight back.
        this._attempts = 0
        void this._open()
        return
      }
      // The session is genuinely over. The next failed REST call already
      // routes the user through onUnauthorized; we just stop consuming.
      this._setState('unauthorized')
      return
    }
    this._scheduleReconnect()
  }

  _scheduleReconnect() {
    if (this._closedByUs) return
    this._attempts += 1
    const base = Math.min(
      this._backoff.initialMs * 2 ** Math.max(0, this._attempts - 1),
      this._backoff.maxMs
    )
    const delay = Math.round(base * (1 - this._backoff.jitter() * 0.5))
    this._setState('reconnecting')
    this._timer = setTimeout(() => {
      this._timer = null
      void this._open()
    }, delay)
  }

  _send(frame) {
    const socket = this._socket
    if (socket && socket.readyState === 1) socket.send(JSON.stringify(frame))
  }
}
