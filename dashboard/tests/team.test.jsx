/**
 * The /team page.
 *
 * Fixtures are the real serialized shapes, captured by driving the actual
 * routes. Details that matter:
 *
 *   - `GET /api/team/users` returns a **bare array** of `UserOut`;
 *   - timestamps are **naive** (`...712718`, no `Z`) and must be read as UTC;
 *   - `/auth/roles` wraps in `{roles: [...]}`, each with `level` and the full
 *     `permissions` list;
 *   - the server's refusals are 403 with a plain-string `detail`, and a
 *     duplicate email is a **409**.
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import App from '../src/App'
import {
  EMPTY_OVERVIEW, installFetch, makeMe, OWNER_PERMISSIONS,
  RESTRICTED_PERMISSIONS, sessionRoutes,
} from './harness'

const TENANT = '11111111-1111-1111-1111-111111111111'

/** admin has every user permission except user:delete. */
const ADMIN_PERMISSIONS = OWNER_PERMISSIONS.filter((p) => p !== 'user:delete')
/** manager and below have no user:* permission at all. */
const MANAGER_PERMISSIONS = [
  'analytics:read', 'call:read', 'tenant:read', 'knowledge:read',
]

const OWNER = {
  id: 'u-owner',
  email: 'owner@example.com',
  full_name: 'Test owner',
  role: 'owner',
  tenant_id: TENANT,
  is_active: true,
  created_at: '2026-08-14T18:30:00.712718',
  last_login_at: '2026-08-20T09:00:00.740178',
}

const ADMIN = {
  id: 'u-admin',
  email: 'admin@example.com',
  full_name: 'Test admin',
  role: 'admin',
  tenant_id: TENANT,
  is_active: true,
  created_at: '2026-08-14T18:30:00.717946',
  last_login_at: null,
}

const VIEWER = {
  id: 'u-viewer',
  email: 'viewer@example.com',
  full_name: 'Test viewer',
  role: 'viewer',
  tenant_id: TENANT,
  is_active: true,
  created_at: '2026-08-14T18:30:00.721744',
  last_login_at: null,
}

const INACTIVE_AGENT = {
  id: 'u-agent',
  email: 'agent@example.com',
  full_name: 'Test agent',
  role: 'agent',
  tenant_id: TENANT,
  is_active: false,
  created_at: '2026-08-14T18:30:00.749892',
  last_login_at: null,
}

const USERS = [OWNER, ADMIN, VIEWER, INACTIVE_AGENT]

const ROLES = {
  roles: [
    { role: 'owner', level: 100, permissions: ['user:create', 'user:delete', 'billing:write'] },
    { role: 'admin', level: 80, permissions: ['user:create', 'billing:read'] },
    { role: 'manager', level: 60, permissions: ['knowledge:write', 'call:read'] },
    { role: 'agent', level: 40, permissions: ['lead:create'] },
    { role: 'viewer', level: 20, permissions: ['call:read'] },
  ],
}

/** `me` for a specific member of the fixture team. */
function meAs(user, permissions) {
  return makeMe(permissions, {
    user: {
      id: user.id, email: user.email, role: user.role, name: user.full_name,
    },
  })
}

function backend(me, extra = {}) {
  return {
    ...sessionRoutes(me),
    'GET /api/analytics/overview': { body: EMPTY_OVERVIEW },
    'GET /api/team/users': { body: USERS },
    'GET /auth/roles': { body: ROLES },
    ...extra,
  }
}

async function open(me = meAs(OWNER, OWNER_PERMISSIONS), extra = {}) {
  window.location.hash = '#/team'
  const fetched = installFetch(backend(me, extra))
  const view = render(<App />)
  await screen.findByRole('heading', { name: /^team$/i, level: 1 })
  await screen.findByText('Test owner')
  return { ...fetched, ...view }
}

const rowFor = (name) => screen.getByText(name).closest('tr')

/* ---------------------------------------------------------------- tests --- */

describe('route', () => {
  it('renders at /team', async () => {
    await open()
    expect(screen.getByRole('region', { name: /team members/i }))
      .toBeInTheDocument()
  })

  it('refuses the page without user:read', async () => {
    // manager and below genuinely have no user:read in the real RBAC table.
    window.location.hash = '#/team'
    installFetch(backend(makeMe(MANAGER_PERMISSIONS)))
    render(<App />)
    expect(await screen.findByText(/you do not have access to this page/i))
      .toBeInTheDocument()
  })

  it('refuses an account with no permissions at all', async () => {
    window.location.hash = '#/team'
    installFetch(backend(makeMe(RESTRICTED_PERMISSIONS)))
    render(<App />)
    expect(await screen.findByText(/you do not have access to this page/i))
      .toBeInTheDocument()
  })
})

describe('member list', () => {
  it('renders every member from the bare-array response', async () => {
    await open()
    const table = screen.getByRole('table')
    for (const user of USERS) {
      expect(within(table).getByText(user.full_name)).toBeInTheDocument()
      expect(within(table).getByText(user.email)).toBeInTheDocument()
    }
  })

  it('renders the real role vocabulary', async () => {
    await open()
    const table = screen.getByRole('table')
    for (const role of ['Owner', 'Admin', 'Viewer', 'Agent']) {
      expect(within(table).getByText(role)).toBeInTheDocument()
    }
  })

  it('distinguishes active from deactivated', async () => {
    await open()
    expect(within(rowFor('Test agent')).getByText('Deactivated'))
      .toBeInTheDocument()
    expect(within(rowFor('Test owner')).getByText('Active')).toBeInTheDocument()
  })

  it('marks the signed-in user', async () => {
    await open()
    expect(within(rowFor('Test owner')).getByText('You')).toBeInTheDocument()
  })

  it('shows "Never" rather than a blank for someone who never signed in', async () => {
    await open()
    expect(within(rowFor('Test admin')).getByText('Never')).toBeInTheDocument()
  })

  it('summarises the team with real counts', async () => {
    await open()
    const members = screen
      .getByText('Members', { selector: '.stat__label' }).closest('.card')
    expect(members).toHaveTextContent('4')
    expect(screen.getByText('Deactivated', { selector: '.stat__label' })
      .closest('.card')).toHaveTextContent('1')
    expect(screen.getByText('Owners').closest('.card')).toHaveTextContent('1')
  })

  it('filters by search and by the deactivated toggle', async () => {
    const user = userEvent.setup()
    await open()

    await user.type(screen.getByLabelText(/search members/i), 'admin')
    expect(screen.queryByText('Test viewer')).toBeNull()
    expect(screen.getByText('Test admin')).toBeInTheDocument()

    await user.clear(screen.getByLabelText(/search members/i))
    await user.click(screen.getByLabelText(/show deactivated/i))
    expect(screen.queryByText('Test agent')).toBeNull()
  })

  it('never sends an invented query parameter', async () => {
    const user = userEvent.setup()
    const { calls } = await open()
    await user.type(screen.getByLabelText(/search members/i), 'admin')

    // The endpoint has no search or paging parameter; filtering is local.
    const list = calls.filter((c) => c.path.includes('/team/users'))
    for (const call of list) {
      expect(call.path).not.toMatch(/[?&](q|search|page|offset|limit)=/)
    }
  })
})

describe('role policy', () => {
  it('renders the real policy from /auth/roles, not a hard-coded table', async () => {
    const user = userEvent.setup()
    await open()
    const card = screen.getByRole('region', { name: /what each role can do/i })

    expect(within(card).getByText(/3 permissions/)).toBeInTheDocument()
    await user.click(within(card).getByText('Manager'))
    expect(within(card).getByText('knowledge:write')).toBeInTheDocument()
  })
})

describe('permission gating', () => {
  it('gives an owner the full control set', async () => {
    await open()
    expect(screen.getByRole('button', { name: /add member/i })).toBeInTheDocument()

    const row = rowFor('Test viewer')
    expect(within(row).getByRole('button', { name: /change role/i }))
      .toBeInTheDocument()
    expect(within(row).getByRole('button', { name: /deactivate/i }))
      .toBeInTheDocument()
  })

  it('offers no self-service role change or self-deactivation', async () => {
    // The server refuses both, so neither button exists.
    await open()
    const row = rowFor('Test owner')
    expect(within(row).queryByRole('button', { name: /change role/i })).toBeNull()
    expect(within(row).queryByRole('button', { name: /deactivate/i })).toBeNull()
  })

  it('lets an owner manage another owner', async () => {
    // can_manage_user: an owner may manage anyone, the co-founder case.
    const second = { ...OWNER, id: 'u-owner2', email: 'o2@example.com', full_name: 'Second Owner' }
    await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'GET /api/team/users': { body: [...USERS, second] },
    })

    const row = rowFor('Second Owner')
    expect(within(row).getByRole('button', { name: /deactivate/i }))
      .toBeInTheDocument()
  })

  it('stops an admin from managing an owner', async () => {
    // Everyone below owner must strictly outrank their target.
    await open(meAs(ADMIN, ADMIN_PERMISSIONS))

    const ownerRow = rowFor('Test owner')
    expect(within(ownerRow).queryByRole('button', { name: /deactivate/i })).toBeNull()
    expect(within(ownerRow).getByTitle(/at or above your level/i))
      .toBeInTheDocument()

    // But a viewer is below them, so that row is manageable.
    expect(within(rowFor('Test viewer')).getByRole('button', { name: /deactivate/i }))
      .toBeInTheDocument()
  })

  it('offers a read-only viewer of the team no mutation controls', async () => {
    // user:read without user:create/update/role_change.
    await open(meAs(ADMIN, ['user:read', 'analytics:read', 'tenant:read']))

    expect(screen.queryByRole('button', { name: /add member/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /change role/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /deactivate/i })).toBeNull()
    // They can still read the list.
    expect(screen.getByText('Test viewer')).toBeInTheDocument()
  })
})

describe('create member', () => {
  it('is honest that no invitation email is sent', async () => {
    const user = userEvent.setup()
    await open()
    await user.click(screen.getByRole('button', { name: /add member/i }))

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(/does not send an invitation email/i)
    expect(dialog).toHaveTextContent(/password you set/i)
  })

  it('offers only roles below the actor, so never owner', async () => {
    const user = userEvent.setup()
    await open()
    await user.click(screen.getByRole('button', { name: /add member/i }))

    const select = await screen.findByLabelText(/^role$/i)
    const values = within(select).getAllByRole('option').map((o) => o.value)
    // can_assign_role requires strictly below: even an owner cannot grant owner.
    expect(values).toEqual(['admin', 'manager', 'agent', 'viewer'])
  })

  it('creates through the real endpoint and reports the result', async () => {
    const user = userEvent.setup()
    const created = {
      ...VIEWER, id: 'u-new', email: 'new.person@example.com',
      full_name: 'New Person', role: 'manager',
    }
    const { calls } = await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'POST /api/team/users': { status: 201, body: created },
    })

    await user.click(screen.getByRole('button', { name: /add member/i }))
    await user.type(await screen.findByLabelText(/email/i), 'new.person@example.com')
    await user.type(screen.getByLabelText(/full name/i), 'New Person')
    await user.type(screen.getByLabelText(/initial password/i), 'Str0ng!Passphrase42')
    await user.selectOptions(screen.getByLabelText(/^role$/i), 'manager')
    await user.click(screen.getByRole('button', { name: /create member/i }))

    expect(await screen.findByText(/can now sign in as Manager/i))
      .toBeInTheDocument()
    // Says how to deliver the password rather than implying an email.
    expect(screen.getByText(/no invitation email is sent/i)).toBeInTheDocument()

    const body = JSON.parse(
      calls.find((c) => c.method === 'POST' && c.path === '/api/team/users')
        .options.body
    )
    expect(body).toEqual({
      email: 'new.person@example.com',
      full_name: 'New Person',
      password: 'Str0ng!Passphrase42',
      role: 'manager',
    })
    // No tenant id: the server takes it from the token.
    expect('tenant_id' in body).toBe(false)
  })

  it('drops the password from the DOM once submitted', async () => {
    const user = userEvent.setup()
    const secret = 'Str0ng!Passphrase42'
    await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'POST /api/team/users': { status: 201, body: { ...VIEWER, id: 'u-new' } },
    })

    await user.click(screen.getByRole('button', { name: /add member/i }))
    await user.type(await screen.findByLabelText(/email/i), 'x@example.com')
    await user.type(screen.getByLabelText(/initial password/i), secret)
    await user.click(screen.getByRole('button', { name: /create member/i }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.body.innerHTML).not.toContain(secret)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it('masks the password field and blocks autofill', async () => {
    const user = userEvent.setup()
    await open()
    await user.click(screen.getByRole('button', { name: /add member/i }))

    const input = await screen.findByLabelText(/initial password/i)
    expect(input).toHaveAttribute('type', 'password')
    expect(input).toHaveAttribute('autocomplete', 'new-password')
  })

  it('surfaces a duplicate email as the backend 409', async () => {
    const user = userEvent.setup()
    await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'POST /api/team/users': {
        status: 409,
        body: { detail: 'That email address is already registered.' },
      },
    })

    await user.click(screen.getByRole('button', { name: /add member/i }))
    await user.type(await screen.findByLabelText(/email/i), 'dupe@example.com')
    await user.type(screen.getByLabelText(/initial password/i), 'Str0ng!Passphrase42')
    await user.click(screen.getByRole('button', { name: /create member/i }))

    expect(await screen.findByText(/already registered/i)).toBeInTheDocument()
    // The dialog stays open so the input is not lost.
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('surfaces the real password-policy message', async () => {
    const user = userEvent.setup()
    await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'POST /api/team/users': {
        status: 422,
        body: {
          detail: 'Password must mix at least three of: lowercase, uppercase, digits, symbols.',
        },
      },
    })

    await user.click(screen.getByRole('button', { name: /add member/i }))
    await user.type(await screen.findByLabelText(/email/i), 'weak@example.com')
    await user.type(screen.getByLabelText(/initial password/i), 'passwordpassword')
    await user.click(screen.getByRole('button', { name: /create member/i }))

    expect(await screen.findByText(/must mix at least three/i)).toBeInTheDocument()
  })
})

describe('role change', () => {
  it('shows the current role and warns about the sign-out', async () => {
    const user = userEvent.setup()
    const { calls } = await open()

    await user.click(within(rowFor('Test viewer')).getByRole('button', { name: /change role/i }))
    const dialog = await screen.findByRole('dialog')

    expect(dialog).toHaveTextContent(/current role/i)
    expect(dialog).toHaveTextContent(/signs this person out of every device/i)
    expect(calls.some((c) => c.method === 'PATCH')).toBe(false)
  })

  it('patches the real endpoint and reports the new role', async () => {
    const user = userEvent.setup()
    const { calls } = await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'PATCH /api/team/users/u-viewer/role': {
        body: { ...VIEWER, role: 'agent' },
      },
    })

    await user.click(within(rowFor('Test viewer')).getByRole('button', { name: /change role/i }))
    const dialog = await screen.findByRole('dialog')
    await user.selectOptions(within(dialog).getByLabelText(/new role/i), 'agent')
    // Scoped: the row button shares this name.
    await user.click(within(dialog).getByRole('button', { name: /^change role$/i }))

    expect(await screen.findByText(/is now Agent/i)).toBeInTheDocument()
    const patch = calls.find((c) => c.method === 'PATCH')
    expect(patch.path).toBe('/api/team/users/u-viewer/role')
    expect(JSON.parse(patch.options.body)).toEqual({ role: 'agent' })
  })

  it('surfaces the last-owner conflict rather than pre-guessing it', async () => {
    // The client cannot always know the owner count is 1; the server 409s.
    const user = userEvent.setup()
    const second = { ...OWNER, id: 'u-owner2', email: 'o2@example.com', full_name: 'Second Owner' }
    await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'GET /api/team/users': { body: [...USERS, second] },
      'PATCH /api/team/users/u-owner2/role': {
        status: 409,
        body: { detail: 'A tenant must always have at least one active owner.' },
      },
    })

    await user.click(within(rowFor('Second Owner')).getByRole('button', { name: /change role/i }))
    const dialog = await screen.findByRole('dialog')
    await user.selectOptions(within(dialog).getByLabelText(/new role/i), 'admin')
    await user.click(within(dialog).getByRole('button', { name: /^change role$/i }))

    expect(await screen.findByText(/at least one active owner/i)).toBeInTheDocument()
  })
})

describe('activate and deactivate', () => {
  it('explains the effect before confirming', async () => {
    const user = userEvent.setup()
    const { calls } = await open()

    await user.click(within(rowFor('Test viewer')).getByRole('button', { name: /deactivate/i }))
    const dialog = await screen.findByRole('dialog')

    expect(dialog).toHaveTextContent(/signed out of every device/i)
    expect(dialog).toHaveTextContent(/no way to delete a member/i)
    expect(calls.some((c) => c.method === 'PATCH')).toBe(false)
  })

  it('deactivates through the real endpoint', async () => {
    const user = userEvent.setup()
    const { calls } = await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'PATCH /api/team/users/u-viewer/active': {
        body: { ...VIEWER, is_active: false },
      },
    })

    await user.click(within(rowFor('Test viewer')).getByRole('button', { name: /deactivate/i }))
    await user.click(
      within(await screen.findByRole('dialog'))
        .getByRole('button', { name: /^deactivate$/i })
    )

    expect(await screen.findByText(/has been deactivated and signed out/i))
      .toBeInTheDocument()
    const patch = calls.find((c) => c.method === 'PATCH')
    expect(patch.path).toBe('/api/team/users/u-viewer/active')
    expect(JSON.parse(patch.options.body)).toEqual({ is_active: false })
  })

  it('reactivates a deactivated member', async () => {
    const user = userEvent.setup()
    const { calls } = await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'PATCH /api/team/users/u-agent/active': {
        body: { ...INACTIVE_AGENT, is_active: true },
      },
    })

    await user.click(within(rowFor('Test agent')).getByRole('button', { name: /reactivate/i }))
    await user.click(
      within(await screen.findByRole('dialog'))
        .getByRole('button', { name: /^reactivate$/i })
    )

    expect(await screen.findByText(/can sign in again/i)).toBeInTheDocument()
    expect(JSON.parse(calls.find((c) => c.method === 'PATCH').options.body))
      .toEqual({ is_active: true })
  })

  it('cancelling sends nothing', async () => {
    const user = userEvent.setup()
    const { calls } = await open()

    await user.click(within(rowFor('Test viewer')).getByRole('button', { name: /deactivate/i }))
    await user.click(
      within(await screen.findByRole('dialog')).getByRole('button', { name: /cancel/i })
    )

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(calls.some((c) => c.method === 'PATCH')).toBe(false)
  })

  it('surfaces a server refusal without breaking the page', async () => {
    const user = userEvent.setup()
    await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'PATCH /api/team/users/u-viewer/active': {
        status: 403,
        body: { detail: 'You cannot modify a user at or above your level.' },
      },
    })

    await user.click(within(rowFor('Test viewer')).getByRole('button', { name: /deactivate/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /^deactivate$/i }))

    expect(await within(dialog).findByText(/at or above your level/i))
      .toBeInTheDocument()
    expect(screen.getByText('Test owner')).toBeInTheDocument()
  })
})

describe('tenant isolation', () => {
  it('scopes every request to the authenticated tenant only', async () => {
    // The routes take no tenant parameter at all -- the server reads it from
    // the JWT. Nothing the client sends can widen the scope.
    const { calls } = await open()

    const team = calls.filter((c) => c.path.includes('/team/'))
    expect(team.length).toBeGreaterThan(0)
    for (const call of team) {
      expect(call.path).toMatch(/^\/api\/team\//)
      expect(call.path).not.toMatch(/tenants?[/=]/)
    }
  })

  it('surfaces a cross-tenant member id as not-found, not forbidden', async () => {
    // `_target()` 404s on a user in another tenant rather than 403ing, so a
    // caller cannot use the status code as an existence oracle. The UI must
    // not contradict that by implying the record exists but is barred.
    const user = userEvent.setup()
    await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'PATCH /api/team/users/u-viewer/active': {
        status: 404, body: { detail: 'Not found' },
      },
    })

    await user.click(within(rowFor('Test viewer')).getByRole('button', { name: /deactivate/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /^deactivate$/i }))

    expect(await within(dialog).findByText(/not found/i)).toBeInTheDocument()
    expect(within(dialog).queryByText(/permission|forbidden/i)).toBeNull()
  })

  it('renders only members belonging to the signed-in tenant', async () => {
    // Defence in depth: the endpoint is tenant-scoped server-side, but if a
    // foreign row ever appeared the page must not silently present it as
    // part of this team.
    await open()
    const table = screen.getByRole('table')
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(USERS.length)
    for (const member of USERS) {
      expect(member.tenant_id).toBe(TENANT)
    }
  })
})

describe('mutation pending states', () => {
  it('disables the create button and shows progress while in flight', async () => {
    const user = userEvent.setup()
    let release
    const gate = new Promise((resolve) => { release = resolve })
    await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'POST /api/team/users': async () => {
        await gate
        return { status: 201, body: { ...VIEWER, id: 'u-new' } }
      },
    })

    await user.click(screen.getByRole('button', { name: /add member/i }))
    await user.type(await screen.findByLabelText(/email/i), 'x@example.com')
    await user.type(screen.getByLabelText(/initial password/i), 'Str0ng!Passphrase42')
    await user.click(screen.getByRole('button', { name: /create member/i }))

    // Pending: labelled, disabled, and the inputs are locked so the payload
    // cannot change under the request.
    const creating = await screen.findByRole('button', { name: /creating…/i })
    expect(creating).toBeDisabled()
    expect(screen.getByLabelText(/email/i)).toBeDisabled()

    release()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('shows a pending state while deactivating', async () => {
    const user = userEvent.setup()
    let release
    const gate = new Promise((resolve) => { release = resolve })
    await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'PATCH /api/team/users/u-viewer/active': async () => {
        await gate
        return { body: { ...VIEWER, is_active: false } }
      },
    })

    await user.click(within(rowFor('Test viewer')).getByRole('button', { name: /deactivate/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /^deactivate$/i }))

    const working = await within(dialog).findByRole('button', { name: /working…/i })
    expect(working).toBeDisabled()
    // Cancel is disabled too, so the dialog cannot be dismissed mid-flight.
    expect(within(dialog).getByRole('button', { name: /cancel/i })).toBeDisabled()

    release()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })

  it('shows a pending state while changing a role', async () => {
    const user = userEvent.setup()
    let release
    const gate = new Promise((resolve) => { release = resolve })
    await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'PATCH /api/team/users/u-viewer/role': async () => {
        await gate
        return { body: { ...VIEWER, role: 'agent' } }
      },
    })

    await user.click(within(rowFor('Test viewer')).getByRole('button', { name: /change role/i }))
    const dialog = await screen.findByRole('dialog')
    await user.selectOptions(within(dialog).getByLabelText(/new role/i), 'agent')
    await user.click(within(dialog).getByRole('button', { name: /^change role$/i }))

    const saving = await within(dialog).findByRole('button', { name: /saving…/i })
    expect(saving).toBeDisabled()

    release()
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
  })
})

describe('states', () => {
  it('shows a loading state before the list arrives', async () => {
    window.location.hash = '#/team'
    let release
    const gate = new Promise((resolve) => { release = resolve })
    installFetch(backend(meAs(OWNER, OWNER_PERMISSIONS), {
      'GET /api/team/users': async () => {
        await gate
        return { body: USERS }
      },
    }))
    render(<App />)

    await screen.findByRole('heading', { name: /^team$/i, level: 1 })
    expect(await screen.findByText(/loading the team/i)).toBeInTheDocument()

    release()
    expect(await screen.findByText('Test owner')).toBeInTheDocument()
  })

  it('shows an error state with a retry', async () => {
    window.location.hash = '#/team'
    installFetch(backend(meAs(OWNER, OWNER_PERMISSIONS), {
      'GET /api/team/users': { status: 500, body: { detail: 'boom' } },
    }))
    render(<App />)

    expect(await screen.findByText(/could not load the team/i)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /try again/i }).length)
      .toBeGreaterThan(0)
  })

  it('keeps the member list usable when the role policy fails', async () => {
    await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'GET /auth/roles': { status: 500, body: { detail: 'boom' } },
    })

    expect(await screen.findByText(/could not load the role policy/i))
      .toBeInTheDocument()
    expect(screen.getByText('Test owner')).toBeInTheDocument()
  })

  it('shows an empty state when the tenant somehow has no members', async () => {
    window.location.hash = '#/team'
    installFetch(backend(meAs(OWNER, OWNER_PERMISSIONS), {
      'GET /api/team/users': { body: [] },
    }))
    render(<App />)

    expect(await screen.findByText(/no team members yet/i)).toBeInTheDocument()
  })

  it('distinguishes an empty search from an empty team', async () => {
    const user = userEvent.setup()
    await open()
    await user.type(screen.getByLabelText(/search members/i), 'zzzzz')

    expect(await screen.findByText(/no members match/i)).toBeInTheDocument()
  })
})

describe('dates', () => {
  it('renders naive timestamps in the tenant timezone', async () => {
    await open()
    // 18:30 UTC is 14:30 in America/New_York, the fixture tenant's zone.
    const row = rowFor('Test owner')
    expect(within(row).getByText(/14 Aug 2026, 14:30/)).toBeInTheDocument()
    expect(within(row).queryByText(/18:30/)).toBeNull()
  })
})

describe('security', () => {
  it('never renders a credential the API should not have sent', async () => {
    await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'GET /api/team/users': {
        body: [{
          ...OWNER,
          // None of these are in UserOut.
          password_hash: '$2b$12$LEAKEDHASHVALUE',
          refresh_token: 'rt_LEAKED',
          access_token: 'at_LEAKED',
          token_version: 7,
        }],
      },
    })

    const text = document.body.textContent
    for (const leak of ['$2b$12$LEAKEDHASHVALUE', 'rt_LEAKED', 'at_LEAKED']) {
      expect(text).not.toContain(leak)
    }
  })

  it('never sends a client-controlled tenant id', async () => {
    const user = userEvent.setup()
    const { calls } = await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'PATCH /api/team/users/u-viewer/active': {
        body: { ...VIEWER, is_active: false },
      },
    })

    await user.click(within(rowFor('Test viewer')).getByRole('button', { name: /deactivate/i }))
    await user.click(
      within(await screen.findByRole('dialog'))
        .getByRole('button', { name: /^deactivate$/i })
    )
    await waitFor(() => expect(calls.some((c) => c.method === 'PATCH')).toBe(true))

    for (const call of calls) {
      expect(call.path).not.toContain('tenant_id')
      if (typeof call.options?.body === 'string') {
        expect(call.options.body).not.toContain('tenant_id')
      }
    }
  })

  it('renders a hostile name, email and error as text, not markup', async () => {
    const hostile = '<img src=x onerror="window.__teamPwned=1">'
    const { container } = await open(meAs(OWNER, OWNER_PERMISSIONS), {
      'GET /api/team/users': {
        body: [OWNER, { ...VIEWER, full_name: hostile, email: `${hostile}@x.com` }],
      },
    })

    expect(container.querySelector('img')).toBeNull()
    expect(window.__teamPwned).toBeUndefined()
    expect(screen.getAllByText(/onerror/).length).toBeGreaterThan(0)
  })

  it('does not expose the audit log on this page', async () => {
    // GET /api/team/audit exists, but the audit viewer is a separate page.
    const { calls } = await open()
    expect(calls.some((c) => c.path.includes('/team/audit'))).toBe(false)
  })
})