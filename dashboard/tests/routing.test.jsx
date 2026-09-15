/**
 * Routing and permission gating.
 *
 * The audit found the Shell, the router and seven pages present but imported
 * by nothing. These assert they are actually reachable, that navigation does
 * not reload the document, and that a page the account cannot use is not
 * rendered.
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import App, { resolveRoute } from '../src/App'
import { visibleNav } from '../src/components/Shell'
import { makeCan } from '../src/lib/permissions'
import {
  EMPTY_OVERVIEW, installFetch, makeMe, OWNER_PERMISSIONS,
  RESTRICTED_PERMISSIONS, sessionRoutes,
} from './harness'

const EMPTY_CALLS = { calls: [], total: 0, limit: 25, offset: 0 }

function backend(me = makeMe()) {
  return {
    ...sessionRoutes(me),
    'GET /api/analytics/overview': { body: EMPTY_OVERVIEW },
    'GET /api/analytics/calls': {
      body: {
        window: EMPTY_OVERVIEW.window,
        by_status: [], by_direction: [], daily: [], hourly: [],
      },
    },
    'GET /api/analytics/conversion': {
      body: { window: EMPTY_OVERVIEW.window, stages: [] },
    },
    'GET /api/analytics/usage': { status: 403, body: { detail: 'no' } },
    'GET /api/tenants/': { body: EMPTY_CALLS },
    'GET /api/appointments': { body: { items: [], total: 0 } },
  }
}

async function boot(me = makeMe(), hash = '') {
  window.location.hash = hash
  installFetch(backend(me))
  render(<App />)
  await screen.findByRole('navigation', { name: /main navigation/i })
}

describe('route table', () => {
  it.each([
    ['/', 'Overview'],
    ['/overview', 'Overview'],
    ['/calls', 'Calls'],
    ['/calls/abc-123', 'Call detail'],
    ['/leads', 'Leads'],
    ['/appointments', 'Appointments'],
    ['/campaigns', 'Campaigns'],
    ['/analytics', 'Analytics'],
  ])('resolves %s', (path, title) => {
    const resolved = resolveRoute(path)
    expect(resolved).not.toBeNull()
    expect(resolved.route.title).toBe(title)
  })

  it('captures the call id from /calls/:id', () => {
    expect(resolveRoute('/calls/abc-123').params.id).toBe('abc-123')
  })

  it('resolves the phase 2 pages built so far', () => {
    expect(resolveRoute('/agent').route.title).toBe('Agent')
    expect(resolveRoute('/knowledge').route.title).toBe('Knowledge')
    expect(resolveRoute('/integrations').route.title).toBe('Integrations')
    expect(resolveRoute('/billing').route.title).toBe('Billing')
    expect(resolveRoute('/team').route.title).toBe('Team')
    expect(resolveRoute('/audit').route.title).toBe('Audit log')
  })

  it('leaves no sidebar entry pointing at a missing page', () => {
    // Phase 2 is complete: every nav destination now resolves. This replaces
    // the old "still absent" assertion, which would otherwise quietly pass
    // forever on an empty list.
    const paths = [
      '/', '/calls', '/leads', '/appointments', '/campaigns', '/analytics',
      '/agent', '/knowledge', '/integrations', '/billing', '/team', '/audit',
    ]
    for (const path of paths) {
      expect(resolveRoute(path), `${path} should resolve`).not.toBeNull()
    }
  })
})

describe('navigation', () => {
  it('renders the overview at the root route', async () => {
    await boot()
    expect(await screen.findByRole('heading', { name: /overview/i, level: 1 }))
      .toBeInTheDocument()
  })

  it('navigates to Calls without reloading the document', async () => {
    const user = userEvent.setup()
    await boot()

    const nav = screen.getByRole('navigation', { name: /main navigation/i })
    await user.click(within(nav).getByRole('link', { name: /calls/i }))

    // A hash change, not a navigation: the app is still the same mounted tree.
    await waitFor(() => expect(window.location.hash).toBe('#/calls'))
    expect(await screen.findByRole('heading', { name: /^calls$/i, level: 1 }))
      .toBeInTheDocument()
    // The shell survived, which it would not have across a real page load.
    expect(screen.getByRole('navigation', { name: /main navigation/i }))
      .toBeInTheDocument()
  })

  it('shows a not-found panel for an unknown route', async () => {
    await boot(makeMe(), '#/nope')
    expect(await screen.findByText(/that page does not exist/i))
      .toBeInTheDocument()
  })
})

describe('permission-gated navigation', () => {
  it('hides nav links the account has no permission for', () => {
    const sections = visibleNav(makeCan(RESTRICTED_PERMISSIONS))
    const labels = sections.flatMap((s) => s.items.map((i) => i.label))

    expect(labels).toContain('Overview')
    expect(labels).toContain('Calls')
    // A viewer has neither of these.
    expect(labels).not.toContain('Billing')
    expect(labels).not.toContain('Team')
    expect(labels).not.toContain('Leads')
  })

  it('shows an owner the full navigation', () => {
    const labels = visibleNav(makeCan(OWNER_PERMISSIONS))
      .flatMap((s) => s.items.map((i) => i.label))
    expect(labels).toEqual(
      expect.arrayContaining(['Overview', 'Calls', 'Leads', 'Appointments',
        'Campaigns', 'Analytics', 'Billing', 'Team'])
    )
  })

  it('refuses to render a page the account lacks permission for', async () => {
    // A viewer has no lead:read, so /leads must not render the Leads page
    // even though the URL was typed directly.
    await boot(makeMe(RESTRICTED_PERMISSIONS), '#/leads')

    expect(await screen.findByText(/you do not have access to this page/i))
      .toBeInTheDocument()
  })

  it('does not redirect away from a forbidden page', async () => {
    await boot(makeMe(RESTRICTED_PERMISSIONS), '#/leads')
    await screen.findByText(/you do not have access to this page/i)
    // The URL is preserved so the link stays diagnosable.
    expect(window.location.hash).toBe('#/leads')
  })
})