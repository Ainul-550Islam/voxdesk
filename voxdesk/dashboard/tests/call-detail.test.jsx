/**
 * CallDetail: reachability, permissions and tenant isolation.
 */
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import App from '../src/App'
import {
  EMPTY_OVERVIEW, installFetch, makeMe, RESTRICTED_PERMISSIONS,
  sessionRoutes, TENANT_B,
} from './harness'

const CALL = {
  id: 'call-1',
  call_sid: 'CA123',
  direction: 'inbound',
  from: '+15551234567',
  to: '+15551230000',
  status: 'completed',
  started_at: '2026-08-14T18:30:00Z',
  ended_at: '2026-08-14T18:52:00Z',
  duration: 1320,
  booked: true,
  escalated: false,
  intent: 'book_appointment',
  lead_score: 82,
  llm_used: 'gpt-4o',
  summary: 'Caller booked a cleaning.',
  has_recording: false,
  transfer: { state: 'none' },
  appointment: null,
  lead: null,
  crm_syncs: [],
}

const TRANSCRIPT = [
  { id: 't1', speaker: 'user', text: 'Hi, can I book a cleaning?',
    created_at: '2026-08-14T18:30:05Z' },
  { id: 't2', speaker: 'assistant', text: 'Of course, I can help with that.',
    created_at: '2026-08-14T18:30:09Z' },
]

function backend(me, extra = {}) {
  return {
    ...sessionRoutes(me),
    'GET /api/analytics/overview': { body: EMPTY_OVERVIEW },
    // The real envelope key is `calls`, matching `list_calls` in
    // `app/api/routes.py` -- not `items`.
    'GET /api/tenants/': { body: { calls: [CALL], total: 1, limit: 25, offset: 0 } },
    'GET /api/calls/call-1/transcript': { body: TRANSCRIPT },
    'GET /api/calls/call-1': { body: CALL },
    ...extra,
  }
}

describe('CallDetail', () => {
  it('is reachable by deep link', async () => {
    window.location.hash = '#/calls/call-1'
    installFetch(backend(makeMe()))

    render(<App />)

    expect(await screen.findByRole('heading', { name: /call detail/i }))
      .toBeInTheDocument()
    expect(await screen.findByText('CA123')).toBeInTheDocument()
  })

  it('is reachable by clicking a row in the call log', async () => {
    const user = userEvent.setup()
    window.location.hash = '#/calls'
    installFetch(backend(makeMe()))

    render(<App />)

    // The "When" cell is the row button.
    const rowButton = await screen.findByRole('button', { name: /14 Aug 2026/ })
    await user.click(rowButton)

    await waitFor(() => expect(window.location.hash).toBe('#/calls/call-1'))
    expect(await screen.findByRole('heading', { name: /call detail/i }))
      .toBeInTheDocument()
  })

  it('requests the call by id only, never a tenant id from the URL', async () => {
    window.location.hash = '#/calls/call-1'
    const { calls } = installFetch(backend(makeMe()))

    render(<App />)
    await screen.findByText('CA123')

    const detail = calls.find((c) => c.path.startsWith('/api/calls/call-1'))
    expect(detail.path).toBe('/api/calls/call-1')
    // The tenant is derived from the JWT server-side. Nothing in the request
    // path can be swapped to reach another account.
    expect(detail.path).not.toContain(TENANT_B)
    expect(detail.path).not.toContain('tenant')
  })

  it("surfaces another tenant's call as not-found rather than rendering it", async () => {
    // What the server actually does for a cross-tenant id: 404, never data.
    window.location.hash = '#/calls/someone-elses-call'
    installFetch({
      ...sessionRoutes(makeMe()),
      'GET /api/calls/someone-elses-call': {
        status: 404, body: { detail: 'Not found' },
      },
    })

    render(<App />)

    expect(await screen.findByText(/not found/i)).toBeInTheDocument()
    expect(screen.queryByText('CA123')).not.toBeInTheDocument()
  })

  it('does not fetch a transcript without transcript:read', async () => {
    // This account has call:read but not transcript:read.
    window.location.hash = '#/calls/call-1'
    const { calls } = installFetch(backend(makeMe(RESTRICTED_PERMISSIONS)))

    render(<App />)
    await screen.findByText('CA123')

    expect(await screen.findByText(/does not include access to transcripts/i))
      .toBeInTheDocument()
    // Not merely hidden — never requested.
    expect(calls.some((c) => c.path.includes('/transcript'))).toBe(false)
  })

  it('renders the transcript for an account that may read it', async () => {
    window.location.hash = '#/calls/call-1'
    installFetch(backend(makeMe()))

    render(<App />)

    expect(await screen.findByText(/can I book a cleaning/i)).toBeInTheDocument()
  })
})