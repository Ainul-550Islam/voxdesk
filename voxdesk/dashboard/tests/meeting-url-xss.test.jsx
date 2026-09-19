/**
 * `meeting_url` scheme allowlist — the HIGH-1 stored-XSS fix.
 *
 * `meeting_url` is written straight from a calendar provider payload
 * (Google `hangoutLink`, Microsoft `joinUrl`, Cal.com `meetingUrl` **or its
 * free-text `location` field**) and was previously rendered as an `href`
 * with no scheme check. A stored `javascript:alert(1)` was therefore a
 * clickable script URL on two pages.
 *
 * The backend now normalises unusable values away at ingest, but these tests
 * deliberately feed hostile values through the API layer anyway: the
 * frontend must not depend on the backend having been fixed. A response body
 * is not a trust boundary.
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import App from '../src/App'
import { safeExternalUrl } from '../src/lib/format'
import {
  EMPTY_OVERVIEW, installFetch, makeMe, sessionRoutes,
} from './harness'

/** The payload from the audit finding. */
const EXPLOIT = 'javascript:alert(1)'

const HOSTILE = [
  ['javascript:', EXPLOIT],
  ['uppercase javascript:', 'JavaScript:alert(1)'],
  ['whitespace-prefixed javascript:', '  javascript:alert(1)'],
  ['newline-obfuscated javascript:', '\njavascript:alert(1)'],
  ['data:', 'data:text/html,<script>alert(1)</script>'],
  ['vbscript:', 'vbscript:msgbox(1)'],
  ['file:', 'file:///etc/passwd'],
  ['protocol-relative', '//evil.example.com/join'],
  ['plaintext http:', 'http://plain.example.com/join'],
  ['unknown scheme', 'ftp://files.example.com/x'],
  ['malformed', 'not a url at all'],
  ['free-text location', 'Office, 2nd floor'],
  ['empty', ''],
  ['scheme with no host', 'https://'],
]

const VALID = 'https://meet.google.com/abc-defg-hij'

/* ------------------------------------------------------- the unit itself --- */

describe('safeExternalUrl', () => {
  it('passes a real provider URL through byte-for-byte', () => {
    // Not re-serialised: a trailing slash or re-encoded query would send the
    // user somewhere subtly different from what the provider issued.
    for (const url of [
      VALID,
      'https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc',
      'https://calendar.example.com/event/123',
      'https://cal.example.com/join?token=a-b_c.d~e&x=1',
      'https://example.com:8443/join/room',
    ]) {
      expect(safeExternalUrl(url)).toBe(url)
    }
  })

  it.each(HOSTILE)('rejects %s', (_label, url) => {
    expect(safeExternalUrl(url)).toBeNull()
  })

  it('rejects non-string input rather than throwing', () => {
    for (const value of [null, undefined, 42, {}, [], true]) {
      expect(safeExternalUrl(value)).toBeNull()
    }
  })

  it('trims surrounding whitespace on an otherwise valid URL', () => {
    expect(safeExternalUrl('  https://ok.example.com/x  '))
      .toBe('https://ok.example.com/x')
  })
})

/* ----------------------------------------------------------- Appointments --- */

const APPOINTMENT = {
  id: 'appt-1',
  customer_name: 'Dana Reed',
  customer_phone: '+15551234567',
  starts_at: '2026-08-14T18:30:00Z',
  ends_at: '2026-08-14T19:00:00Z',
  timezone: 'America/New_York',
  status: 'confirmed',
  service: 'Cleaning',
  external_event_id: 'evt_1',
  meeting_url: VALID,
}

function appointmentsBackend(meetingUrl) {
  return {
    ...sessionRoutes(makeMe()),
    'GET /api/analytics/overview': { body: EMPTY_OVERVIEW },
    'GET /api/appointments': {
      body: {
        appointments: [{ ...APPOINTMENT, meeting_url: meetingUrl }],
        total: 1, limit: 25, offset: 0,
      },
    },
  }
}

async function openAppointments(meetingUrl) {
  window.location.hash = '#/appointments'
  installFetch(appointmentsBackend(meetingUrl))
  render(<App />)
  await screen.findByText('Dana Reed')
}

describe('Appointments meeting link', () => {
  it('renders a valid https URL as a link to the exact destination', async () => {
    await openAppointments(VALID)
    const link = screen.getByRole('link', { name: /join/i })
    expect(link).toHaveAttribute('href', VALID)
  })

  it('uses noopener and noreferrer on the external link', async () => {
    await openAppointments(VALID)
    const link = screen.getByRole('link', { name: /join/i })
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toMatch(/noopener/)
    expect(link.getAttribute('rel')).toMatch(/noreferrer/)
  })

  it.each(HOSTILE)('renders no link for %s', async (_label, url) => {
    await openAppointments(url)

    // No link at all, and certainly not one carrying the payload.
    expect(screen.queryByRole('link', { name: /join/i })).toBeNull()
    for (const anchor of document.querySelectorAll('a[href]')) {
      expect(anchor.getAttribute('href')).toMatch(/^(#|https:\/\/)/)
    }
  })

  it('still renders the row, falling back to the neutral dash', async () => {
    // Rejecting the URL must not hide the appointment itself.
    await openAppointments(EXPLOIT)
    expect(screen.getByText('Dana Reed')).toBeInTheDocument()
    const row = screen.getByText('Dana Reed').closest('tr')
    expect(within(row).getAllByText('—').length).toBeGreaterThan(0)
  })

  it('does not crash on a malformed URL', async () => {
    await openAppointments('http://[not-a-valid-host')
    expect(screen.getByText('Dana Reed')).toBeInTheDocument()
  })
})

/* -------------------------------------------------------------- CallDetail --- */

const CALL = {
  id: 'call-1', call_sid: 'CA123', direction: 'inbound',
  from: '+15551234567', to: '+15551230000', status: 'completed',
  started_at: '2026-08-14T18:30:00Z', ended_at: '2026-08-14T18:52:00Z',
  duration: 1320, booked: true, escalated: false,
  intent: 'book_appointment', lead_score: 82, llm_used: 'gpt-4o',
  summary: 'Caller booked a cleaning.', has_recording: false,
  transfer: { state: 'none' }, lead: null, crm_syncs: [],
  appointment: {
    id: 'appt-1',
    customer_name: 'Dana Reed',
    starts_at: '2026-08-14T18:30:00Z',
    timezone: 'America/New_York',
    status: 'confirmed',
    meeting_url: VALID,
  },
}

async function openCallDetail(meetingUrl) {
  window.location.hash = '#/calls/call-1'
  const call = {
    ...CALL,
    appointment: { ...CALL.appointment, meeting_url: meetingUrl },
  }
  installFetch({
    ...sessionRoutes(makeMe()),
    'GET /api/analytics/overview': { body: EMPTY_OVERVIEW },
    'GET /api/calls/call-1/transcript': { body: [] },
    'GET /api/calls/call-1': { body: call },
  })
  render(<App />)
  await screen.findByText('Dana Reed')
}

describe('CallDetail meeting link', () => {
  it('renders a valid https URL as a link to the exact destination', async () => {
    await openCallDetail(VALID)
    const link = screen.getByRole('link', { name: /join link/i })
    expect(link).toHaveAttribute('href', VALID)
  })

  it('uses noopener and noreferrer on the external link', async () => {
    await openCallDetail(VALID)
    const link = screen.getByRole('link', { name: /join link/i })
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toMatch(/noopener/)
    expect(link.getAttribute('rel')).toMatch(/noreferrer/)
  })

  it.each(HOSTILE)('renders no link for %s', async (_label, url) => {
    await openCallDetail(url)

    expect(screen.queryByRole('link', { name: /join link/i })).toBeNull()
    for (const anchor of document.querySelectorAll('a[href]')) {
      expect(anchor.getAttribute('href')).toMatch(/^(#|https:\/\/)/)
    }
  })

  it('still renders the appointment when the URL is rejected', async () => {
    await openCallDetail(EXPLOIT)
    expect(screen.getByText('Dana Reed')).toBeInTheDocument()
    // The whole Meeting row is omitted rather than shown as a dead link.
    expect(screen.queryByText('Meeting')).toBeNull()
  })
})

/* ------------------------------------------------------- the exploit itself --- */

describe('the reported payload', () => {
  it('never reaches the DOM as a clickable href on either page', async () => {
    for (const open of [openAppointments, openCallDetail]) {
      document.body.innerHTML = ''
      window.location.hash = '#/'
      await open(EXPLOIT)

      const hrefs = [...document.querySelectorAll('a[href]')]
        .map((a) => a.getAttribute('href'))
      expect(hrefs.some((h) => /^javascript:/i.test(h))).toBe(false)
      expect(hrefs).not.toContain(EXPLOIT)

      await waitFor(() =>
        expect(document.querySelector('a[href^="javascript:"]')).toBeNull())
    }
  })

  it('is not executable even if it is displayed as text somewhere', async () => {
    // If anything ever managed to run the payload, this would fire.
    const fired = vi.fn()
    vi.stubGlobal('alert', fired)

    await openAppointments(EXPLOIT)

    // React escapes text nodes, so no script element is ever created and
    // the payload cannot execute.
    expect(document.querySelector('script')).toBeNull()
    expect(fired).not.toHaveBeenCalled()

    vi.unstubAllGlobals()
  })
})