/**
 * A fake backend at the `fetch` boundary.
 *
 * Deliberately *not* a mock of `lib/api.js`. Stubbing the API module would
 * mean the token handling, the single-retry refresh and the 401 logout path —
 * the parts the audit called out as correct and worth keeping — were never
 * executed by a test. Faking `fetch` instead means the real client code runs.
 */
import { vi } from 'vitest'

export const TENANT_A = '11111111-1111-1111-1111-111111111111'
export const TENANT_B = '22222222-2222-2222-2222-222222222222'

/**
 * Every permission an owner has -- the exact 33 in `ROLE_PERMISSIONS[OWNER]`,
 * verified against the source.
 *
 * This previously listed only 26, silently omitting `user:role_change`,
 * `user:delete`, `integration:sync`, `lead:delete`, `compliance:read`,
 * `compliance:write` and `security:settings`. A fixture that under-reports
 * an owner's rights makes a page look correctly gated when it is not, so
 * this must stay in step with `app/auth/rbac.py`.
 */
export const OWNER_PERMISSIONS = [
  'analytics:read', 'appointment:read', 'appointment:write', 'audit:read',
  'billing:read', 'billing:write', 'call:read', 'call:read_all',
  'campaign:read', 'campaign:run', 'campaign:write', 'compliance:read',
  'compliance:write', 'integration:read', 'integration:sync',
  'integration:write', 'knowledge:delete', 'knowledge:read',
  'knowledge:write', 'lead:create', 'lead:delete', 'lead:read', 'lead:update',
  'recording:read', 'security:settings', 'tenant:read', 'tenant:update',
  'transcript:read', 'user:create', 'user:delete', 'user:read',
  'user:role_change', 'user:update',
]

/**
 * A viewer, matching the server's `_VIEWER` role exactly:
 * `READ_ONLY_OPERATIONAL | {COMPLIANCE_READ}` in `app/auth/rbac.py`.
 *
 * `tenant:read` is in that set -- every role may see how the agent is
 * configured -- while `tenant:update` is admin and above. Getting this wrong
 * would make the read-only tests assert the wrong thing entirely.
 */
export const VIEWER_PERMISSIONS = [
  'tenant:read', 'call:read', 'call:read_all', 'transcript:read',
  'lead:read', 'appointment:read', 'campaign:read', 'knowledge:read',
  'analytics:read', 'compliance:read',
]

/**
 * A deliberately minimal account: analytics and calls only.
 *
 * Not a real server role -- it exists so a test can assert what happens when a
 * permission is genuinely absent, without weakening `VIEWER_PERMISSIONS` back
 * into something the backend would never issue.
 */
export const RESTRICTED_PERMISSIONS = ['analytics:read', 'call:read']

export function makeMe(permissions = OWNER_PERMISSIONS, overrides = {}) {
  return {
    user: { id: 'u-1', email: 'owner@example.com', role: 'owner', name: 'Owner' },
    tenant: {
      id: TENANT_A,
      name: 'Bright Smile Dental',
      industry: 'dental',
      plan: 'pro',
      language: 'en-US',
      // The two fields STEP 8 added to /auth/me.
      timezone: 'America/New_York',
      agent_name: 'Riya',
      twilio_number: '+15551230000',
    },
    permissions,
    ...overrides,
  }
}

export const EMPTY_OVERVIEW = {
  window: {
    start: '2026-08-01T04:00:00Z',
    end: '2026-09-01T04:00:00Z',
    timezone: 'America/New_York',
    label: 'Last 30 days',
  },
  calls: {
    total: 0, answered: 0, missed: 0, failed: 0, transferred: 0,
    average_duration_seconds: 0,
  },
  conversion: { booked: 0, booking_rate: 0, qualified_leads: 0 },
  operations: { appointments: 0, cancellations: 0, no_shows: 0 },
  integrations: { crm_sync_failures: 0, calendar_failures: 0 },
  usage: null,
}

/**
 * Install a fake `fetch`.
 *
 * `routes` maps `'METHOD /path'` (or a path prefix) to a handler returning
 * `{ status?, body? }`. Anything unmatched is a 404, which surfaces as a
 * failing test rather than a silent undefined.
 */
export function installFetch(routes, { onCall } = {}) {
  const calls = []

  const impl = vi.fn(async (url, options = {}) => {
    const method = (options.method ?? 'GET').toUpperCase()
    const path = String(url)
    calls.push({ method, path, options })
    onCall?.({ method, path, options })

    const exact = routes[`${method} ${path}`]
    const handler = exact ?? findPrefix(routes, method, path)

    if (!handler) {
      return jsonResponse(404, { detail: `no fake route for ${method} ${path}` })
    }

    const result = typeof handler === 'function'
      ? await handler({ method, path, options })
      : handler

    const status = result.status ?? 200
    return jsonResponse(status, result.body ?? null)
  })

  vi.stubGlobal('fetch', impl)
  return { calls, impl }
}

function findPrefix(routes, method, path) {
  const key = Object.keys(routes)
    .filter((candidate) => candidate.startsWith(`${method} `))
    .filter((candidate) => path.startsWith(candidate.slice(method.length + 1)))
    // Longest match wins, so `/api/calls/x/transcript` beats `/api/calls/`.
    .sort((a, b) => b.length - a.length)[0]
  return key ? routes[key] : null
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  }
}

/** The session routes every logged-in test needs. */
export function sessionRoutes(me = makeMe()) {
  return {
    'POST /auth/refresh': { body: { access_token: 'access-token-1' } },
    'GET /auth/me': { body: me },
    'POST /auth/logout': { status: 204 },
  }
}