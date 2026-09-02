/**
 * The /audit page.
 *
 * Fixtures are the real serialized shapes, captured by generating genuine
 * events through the real flows (a failed login, a user creation, a role
 * change, a deactivation, an authz denial) and reading the endpoint back.
 * Details that matter:
 *
 *   - the response is a **bare array**, newest first, with **no total count**
 *     and no cursor;
 *   - `created_at` is **naive** (no `Z`) and must be read as UTC;
 *   - `detail` is `{}` for many actions, and shape-varies by action:
 *     `{reason}`, `{role}`, `{from,to}`, `{missing:[...], path}`;
 *   - `ip_address` is `''` for server-side actions, populated for HTTP ones;
 *   - `target_user_id` is null except for team actions;
 *   - `limit` is the only parameter; `?limit=501` is a **422**.
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import App from '../src/App'
import { redact } from '../src/pages/Audit'
import {
  EMPTY_OVERVIEW, installFetch, makeMe, OWNER_PERMISSIONS,
  RESTRICTED_PERMISSIONS, sessionRoutes,
} from './harness'

const TENANT = '11111111-1111-1111-1111-111111111111'
const TARGET = '5dc1b373-f465-4e2b-b439-eb67c0dbbec9'

const EVENTS = [
  {
    id: 'a-1', action: 'user_deactivated', actor_email: 'owner@example.com',
    target_user_id: TARGET, ip_address: '', detail: {},
    created_at: '2026-08-14T18:30:43.551525',
  },
  {
    id: 'a-2', action: 'authz_denied', actor_email: 'viewer@example.com',
    target_user_id: null, ip_address: '127.0.0.1',
    detail: { missing: ['user:read'], path: '/api/team/users' },
    created_at: '2026-08-14T18:29:43.539570',
  },
  {
    id: 'a-3', action: 'login_success', actor_email: 'viewer@example.com',
    target_user_id: null, ip_address: '127.0.0.1', detail: {},
    created_at: '2026-08-14T18:28:43.530425',
  },
  {
    id: 'a-4', action: 'role_changed', actor_email: 'owner@example.com',
    target_user_id: TARGET, ip_address: '',
    detail: { from: 'viewer', to: 'agent' },
    created_at: '2026-08-14T18:27:43.523022',
  },
  {
    id: 'a-5', action: 'login_failure', actor_email: 'owner@example.com',
    target_user_id: null, ip_address: '127.0.0.1',
    detail: { reason: 'bad_password' },
    created_at: '2026-08-14T18:26:43.495728',
  },
]

const USERS = [{
  id: TARGET, email: 'viewer@example.com', full_name: 'Test viewer',
  role: 'viewer', tenant_id: TENANT, is_active: false,
  created_at: '2026-08-01T10:00:00', last_login_at: null,
}]

function backend(me, extra = {}) {
  return {
    ...sessionRoutes(me),
    'GET /api/analytics/overview': { body: EMPTY_OVERVIEW },
    'GET /api/team/audit': { body: EVENTS },
    'GET /api/team/users': { body: USERS },
    ...extra,
  }
}

async function open(me = makeMe(OWNER_PERMISSIONS), extra = {}) {
  window.location.hash = '#/audit'
  const fetched = installFetch(backend(me, extra))
  const view = render(<App />)
  await screen.findByRole('heading', { name: /audit log/i, level: 1 })
  await screen.findByText('Role Changed')
  return { ...fetched, ...view }
}

const rowFor = (id) => screen.getByText(id).closest('tr')

/* ---------------------------------------------------------------- tests --- */

describe('route', () => {
  it('renders at /audit', async () => {
    await open()
    expect(screen.getByRole('region', { name: /audit events/i }))
      .toBeInTheDocument()
  })

  it('is reachable for an admin, who has audit:read', async () => {
    const admin = OWNER_PERMISSIONS.filter((p) => p !== 'user:delete')
    await open(makeMe(admin))
    expect(screen.getByRole('region', { name: /audit events/i }))
      .toBeInTheDocument()
  })
})

describe('permission gating', () => {
  it('refuses a manager, who has no audit:read', async () => {
    window.location.hash = '#/audit'
    installFetch(backend(makeMe(['analytics:read', 'call:read', 'tenant:read'])))
    render(<App />)
    expect(await screen.findByText(/you do not have access to this page/i))
      .toBeInTheDocument()
  })

  it('refuses an account with no permissions at all', async () => {
    window.location.hash = '#/audit'
    installFetch(backend(makeMe(RESTRICTED_PERMISSIONS)))
    render(<App />)
    expect(await screen.findByText(/you do not have access to this page/i))
      .toBeInTheDocument()
  })

  it('still renders when the user list is unavailable', async () => {
    // audit:read without user:read is not a real combination, but the page
    // must degrade to raw ids rather than break.
    await open(makeMe(OWNER_PERMISSIONS), {
      'GET /api/team/users': { status: 403, body: { detail: 'nope' } },
    })
    expect(screen.getByText('Role Changed')).toBeInTheDocument()
    expect(screen.getAllByTitle(TARGET).length).toBeGreaterThan(0)
  })
})

describe('event rendering', () => {
  it('renders every event from the bare-array response', async () => {
    await open()
    const table = screen.getByRole('table')
    expect(within(table).getAllByRole('row')).toHaveLength(EVENTS.length + 1)
  })

  it('renders the real action vocabulary, humanised', async () => {
    await open()
    const table = screen.getByRole('table')
    for (const label of ['User Deactivated', 'Authz Denied', 'Login Success',
      'Role Changed', 'Login Failure']) {
      expect(within(table).getByText(label)).toBeInTheDocument()
    }
  })

  it('renders the actor email for each event', async () => {
    await open()
    const table = screen.getByRole('table')
    expect(within(table).getAllByText('owner@example.com').length).toBe(3)
  })

  it('resolves a target user id to an email', async () => {
    await open()
    // TARGET is in the user list, so it should read as a person.
    expect(within(rowFor('Role Changed')).getByText('viewer@example.com'))
      .toBeInTheDocument()
  })

  it('shows an unresolvable target as a truncated id, not a guess', async () => {
    await open(makeMe(OWNER_PERMISSIONS), {
      'GET /api/team/users': { body: [] },
    })
    const cell = within(rowFor('Role Changed')).getByTitle(TARGET)
    expect(cell).toHaveTextContent('5dc1b373…')
  })

  it('renders an empty ip_address as a dash, not blank', async () => {
    await open()
    // Server-side actions carry ip_address: ''.
    expect(within(rowFor('Role Changed')).getAllByText('—').length)
      .toBeGreaterThan(0)
    expect(within(rowFor('Authz Denied')).getByText('127.0.0.1'))
      .toBeInTheDocument()
  })

  it('counts only what it can actually count', async () => {
    await open()
    // No total is available, so the label must not claim one.
    const shown = screen.getByText('Events shown').closest('.card')
    expect(shown).toHaveTextContent('5')
    expect(shown).toHaveTextContent(/most recent 100/i)
    expect(document.body.textContent).not.toMatch(/total events/i)

    expect(screen.getByText('Sign-in failures').closest('.card'))
      .toHaveTextContent('1')
    expect(screen.getByText('Permission denials').closest('.card'))
      .toHaveTextContent('1')
  })

  it('shows no risk score or invented severity metric', async () => {
    await open()
    const text = document.body.textContent
    expect(text).not.toMatch(/risk score|threat level|security score/i)
  })
})

describe('details', () => {
  it('hides details behind a toggle and expands on request', async () => {
    const user = userEvent.setup()
    await open()
    const row = rowFor('Role Changed')

    const toggle = within(row).getByRole('button', { name: /show/i })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(within(row).queryByText('viewer')).toBeNull()

    await user.click(toggle)
    expect(within(row).getByRole('button', { name: /hide/i }))
      .toHaveAttribute('aria-expanded', 'true')
    // {from: 'viewer', to: 'agent'}
    expect(within(row).getByText('From')).toBeInTheDocument()
    expect(within(row).getByText('agent')).toBeInTheDocument()
  })

  it('renders a list-valued detail such as authz_denied.missing', async () => {
    const user = userEvent.setup()
    await open()
    const row = rowFor('Authz Denied')

    await user.click(within(row).getByRole('button', { name: /show/i }))
    expect(within(row).getByText('Missing')).toBeInTheDocument()
    expect(within(row).getByText('["user:read"]')).toBeInTheDocument()
    expect(within(row).getByText('/api/team/users')).toBeInTheDocument()
  })

  it('offers no toggle for an event with an empty detail', async () => {
    await open()
    const row = rowFor('Login Success')
    expect(within(row).queryByRole('button', { name: /show/i })).toBeNull()
  })
})

describe('redaction', () => {
  // The unit is exported so the policy can be tested directly, not only
  // through the DOM.
  it('masks credential-shaped keys at any depth', () => {
    const out = redact({
      provider: 'stripe',
      api_key: 'AKIAIOSFODNN7EXAMPLE',
      config: { client_secret: 'shhh', url: 'https://example.com/hook' },
      webhook_signing_secret: 'whsec_x',
      refresh_token: 'rt_x',
      password: 'hunter2',
    })
    expect(out.provider).toBe('stripe')
    expect(out.config.url).toBe('https://example.com/hook')
    expect(out.api_key).toBe('[redacted]')
    expect(out.config.client_secret).toBe('[redacted]')
    expect(out.webhook_signing_secret).toBe('[redacted]')
    expect(out.refresh_token).toBe('[redacted]')
    expect(out.password).toBe('[redacted]')
  })

  it('masks credential-shaped values even under an innocent key', () => {
    const out = redact({
      note: 'sk_live_51H8xQ2eZvKYlo2C',
      jwt: 'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc',
      hash_value: '$2b$12$abcdefghijklmnopqrstuv',
      reason: 'bad_password',
    })
    expect(out.note).toBe('[redacted]')
    expect(out.jwt).toBe('[redacted]')
    expect(out.reason).toBe('bad_password')
  })

  it('preserves ordinary operational detail untouched', () => {
    expect(redact({ from: 'viewer', to: 'agent' }))
      .toEqual({ from: 'viewer', to: 'agent' })
    expect(redact({ missing: ['user:read'], path: '/api/team/users' }))
      .toEqual({ missing: ['user:read'], path: '/api/team/users' })
    expect(redact({ plan: 'pro', interval: 'month' }))
      .toEqual({ plan: 'pro', interval: 'month' })
  })

  it('caps pathological depth rather than hanging', () => {
    let deep = { end: 'value' }
    for (let i = 0; i < 40; i += 1) deep = { nest: deep }
    expect(() => redact(deep)).not.toThrow()
    expect(JSON.stringify(redact(deep))).toContain('too deep')
  })

  it('never paints a leaked secret into the DOM', async () => {
    const user = userEvent.setup()
    await open(makeMe(OWNER_PERMISSIONS), {
      'GET /api/team/audit': {
        body: [{
          ...EVENTS[3],
          detail: {
            provider: 'stripe',
            stripe_secret_key: 'sk_live_LEAKED123',
            refresh_token: 'rt_LEAKED456',
            password_hash: '$2b$12$LEAKEDHASHVALUE0000000',
            webhook_secret: 'whsec_LEAKED789',
          },
        }],
      },
    })

    await user.click(screen.getByRole('button', { name: /show/i }))

    const text = document.body.textContent
    for (const leak of ['sk_live_LEAKED123', 'rt_LEAKED456',
      '$2b$12$LEAKEDHASHVALUE0000000', 'whsec_LEAKED789']) {
      expect(text).not.toContain(leak)
    }
    // Non-secret context survives the redaction.
    expect(text).toContain('stripe')
    expect(screen.getAllByText('[redacted]').length).toBe(4)
  })
})

describe('filters', () => {
  it('says plainly that filtering is client-side', async () => {
    await open()
    expect(screen.getByText(/filtering happen in your browser/i))
      .toBeInTheDocument()
  })

  it('filters by action without calling the server again', async () => {
    const user = userEvent.setup()
    const { calls } = await open()
    const before = calls.filter((c) => c.path.includes('/team/audit')).length

    await user.selectOptions(screen.getByLabelText(/filter by event/i), 'login_failure')

    const table = screen.getByRole('table')
    expect(within(table).getAllByRole('row')).toHaveLength(2)  // header + 1
    expect(within(table).getByText('Login Failure')).toBeInTheDocument()
    // Client-side: no extra request.
    expect(calls.filter((c) => c.path.includes('/team/audit')).length)
      .toBe(before)
  })

  it('searches across actor, action, ip and redacted detail', async () => {
    const user = userEvent.setup()
    await open()
    const box = screen.getByLabelText(/search events/i)

    await user.type(box, 'bad_password')
    expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(2)

    await user.clear(box)
    await user.type(box, 'viewer@example.com')
    // Two events have that actor; a third references it as the target.
    expect(within(screen.getByRole('table')).getAllByRole('row').length)
      .toBeGreaterThan(2)
  })

  it('shows a distinct empty state when a filter matches nothing', async () => {
    const user = userEvent.setup()
    await open()
    await user.type(screen.getByLabelText(/search events/i), 'zzzznomatch')

    expect(await screen.findByText(/no matching events/i)).toBeInTheDocument()
    expect(screen.queryByText(/no audit events yet/i)).toBeNull()
  })

  it('never sends an invented query parameter', async () => {
    const user = userEvent.setup()
    const { calls } = await open()
    await user.type(screen.getByLabelText(/search events/i), 'owner')
    await user.selectOptions(screen.getByLabelText(/filter by event/i), 'role_changed')

    for (const call of calls.filter((c) => c.path.includes('/team/audit'))) {
      // `limit` is the endpoint's only parameter.
      expect(call.path).not.toMatch(/[?&](action|actor|from|to|q|search|offset|page|cursor)=/)
    }
  })
})

describe('limit', () => {
  it('requests the default 100 on first load', async () => {
    const { calls } = await open()
    const call = calls.find((c) => c.path.includes('/team/audit'))
    expect(call.path).toContain('limit=100')
  })

  it('refetches with a larger limit and never exceeds the server ceiling', async () => {
    const user = userEvent.setup()
    const { calls } = await open()

    const select = screen.getByLabelText(/events to load/i)
    const values = within(select).getAllByRole('option').map((o) => Number(o.value))
    // `Query(100, le=500)`: 501 is a 422, so 500 is the highest offered.
    expect(Math.max(...values)).toBe(500)

    await user.selectOptions(select, '500')
    await waitFor(() => expect(
      calls.some((c) => c.path.includes('limit=500'))
    ).toBe(true))
  })

  it('warns that older events may exist when the page looks full', async () => {
    // A full page is a hint of truncation, not proof -- there is no count.
    const full = Array.from({ length: 100 }, (_, i) => ({
      ...EVENTS[3], id: `f-${i}`,
    }))
    // Not via open(): its readiness probe matches 100 identical rows.
    window.location.hash = '#/audit'
    installFetch(backend(makeMe(OWNER_PERMISSIONS), {
      'GET /api/team/audit': { body: full },
    }))
    render(<App />)

    expect(await screen.findByText(/there may be older events/i))
      .toBeInTheDocument()
  })

  it('does not warn when the page is not full', async () => {
    await open()
    expect(screen.queryByText(/there may be older events/i)).toBeNull()
  })
})

describe('view-only', () => {
  it('offers no mutation or export control', async () => {
    await open()
    for (const name of [/delete/i, /remove/i, /edit/i, /export/i, /download/i,
      /clear log/i]) {
      expect(screen.queryByRole('button', { name })).toBeNull()
    }
    expect(screen.getByText(/cannot be edited or removed/i)).toBeInTheDocument()
  })
})

describe('states', () => {
  it('shows a loading state before events arrive', async () => {
    window.location.hash = '#/audit'
    let release
    const gate = new Promise((resolve) => { release = resolve })
    installFetch(backend(makeMe(OWNER_PERMISSIONS), {
      'GET /api/team/audit': async () => {
        await gate
        return { body: EVENTS }
      },
    }))
    render(<App />)

    await screen.findByRole('heading', { name: /audit log/i, level: 1 })
    expect(await screen.findByText(/loading the audit log/i)).toBeInTheDocument()

    release()
    expect(await screen.findByText('Role Changed')).toBeInTheDocument()
  })

  it('shows an error state with a working retry', async () => {
    const user = userEvent.setup()
    window.location.hash = '#/audit'
    let fail = true
    const { calls } = installFetch(backend(makeMe(OWNER_PERMISSIONS), {
      'GET /api/team/audit': async () => (fail
        ? { status: 500, body: { detail: 'boom' } }
        : { body: EVENTS }),
    }))
    render(<App />)

    expect(await screen.findByText(/could not load the audit log/i))
      .toBeInTheDocument()

    fail = false
    await user.click(screen.getAllByRole('button', { name: /try again/i })[0])
    expect(await screen.findByText('Role Changed')).toBeInTheDocument()
    expect(calls.filter((c) => c.path.includes('/team/audit')).length)
      .toBeGreaterThan(1)
  })

  it('shows an empty state for a workspace with no events', async () => {
    window.location.hash = '#/audit'
    installFetch(backend(makeMe(OWNER_PERMISSIONS), {
      'GET /api/team/audit': { body: [] },
    }))
    render(<App />)

    expect(await screen.findByText(/no audit events yet/i)).toBeInTheDocument()
  })
})

describe('dates', () => {
  it('renders naive timestamps in the tenant timezone', async () => {
    await open()
    // 18:30 UTC is 14:30 in America/New_York, the fixture tenant's zone.
    expect(within(rowFor('User Deactivated')).getByText(/14 Aug 2026, 14:30/))
      .toBeInTheDocument()
    expect(within(screen.getByRole('table')).queryByText(/18:30/)).toBeNull()
  })

  it('names the timezone the times are shown in', async () => {
    await open()
    expect(screen.getByText(/America\/New_York/)).toBeInTheDocument()
  })
})

describe('security', () => {
  it('never sends a client-controlled tenant id', async () => {
    const { calls } = await open()
    const audit = calls.filter((c) => c.path.includes('/team/audit'))
    expect(audit.length).toBeGreaterThan(0)
    for (const call of audit) {
      expect(call.path).not.toContain('tenant_id')
      expect(call.path).not.toMatch(/tenants?\//)
      expect(call.method).toBe('GET')
    }
  })

  it('writes nothing to local or session storage', async () => {
    const user = userEvent.setup()
    await open()
    await user.click(screen.getAllByRole('button', { name: /show/i })[0])

    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it('renders hostile actor, action and detail values as text', async () => {
    const user = userEvent.setup()
    const hostile = '<img src=x onerror="window.__auditPwned=1">'
    const { container } = await open(makeMe(OWNER_PERMISSIONS), {
      'GET /api/team/audit': {
        body: [{
          ...EVENTS[3],
          actor_email: `${hostile}@x.com`,
          ip_address: hostile,
          detail: { note: hostile, [hostile]: 'v' },
        }],
      },
    })

    await user.click(screen.getByRole('button', { name: /show/i }))

    expect(container.querySelector('img')).toBeNull()
    expect(window.__auditPwned).toBeUndefined()
    expect(screen.getAllByText(/onerror/).length).toBeGreaterThan(0)
  })

  it('does not expose team management controls on this page', async () => {
    // /audit is a viewer, not an admin console.
    await open()
    expect(screen.queryByRole('button', { name: /add member/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /change role/i })).toBeNull()
  })
})
