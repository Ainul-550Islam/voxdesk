/**
 * Timezone and duration formatting.
 *
 * Findings F11 and F12: timestamps rendered in the browser's timezone and
 * durations rendered as raw seconds. The fixes live in `lib/format.js`; these
 * assert both the helpers and that the mounted pages actually use them.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import App from '../src/App'
import {
  formatDateTime, formatDuration, formatPercent, safeZone,
} from '../src/lib/format'
import { EMPTY_OVERVIEW, installFetch, makeMe, sessionRoutes } from './harness'

describe('formatDuration', () => {
  it('renders a 22-minute call as minutes, not 1320s', () => {
    expect(formatDuration(1320)).toBe('22m')
  })

  it.each([
    [0, '0s'],
    [45, '45s'],
    [60, '1m'],
    [95, '1m 35s'],
    [3600, '1h 0m'],
    [3725, '1h 2m'],
  ])('formats %is as %s', (seconds, expected) => {
    expect(formatDuration(seconds)).toBe(expected)
  })

  it('does not produce NaN for missing input', () => {
    expect(formatDuration(undefined)).toBe('0s')
    expect(formatDuration(null)).toBe('0s')
  })
})

describe('formatDateTime', () => {
  const utc = '2026-08-14T18:30:00Z'

  it("renders in the tenant's timezone, not the browser's", () => {
    // 18:30 UTC is 14:30 in New York and 00:30 the next day in Dhaka.
    expect(formatDateTime(utc, 'America/New_York')).toContain('14:30')
    expect(formatDateTime(utc, 'Asia/Dhaka')).toContain('00:30')
  })

  it('treats a naive backend timestamp as UTC rather than local', () => {
    // SQLite hands back naive values even for tz-aware columns. Assuming
    // local here is how a whole dashboard silently shifts by hours.
    expect(formatDateTime('2026-08-14T18:30:00', 'UTC'))
      .toBe(formatDateTime('2026-08-14T18:30:00Z', 'UTC'))
  })

  it('falls back to UTC for a malformed tenant timezone', () => {
    expect(safeZone('Not/AZone')).toBe('UTC')
    expect(() => formatDateTime(utc, 'Not/AZone')).not.toThrow()
  })

  it('renders an em dash for a missing timestamp', () => {
    expect(formatDateTime(null, 'UTC')).toBe('—')
  })
})

describe('formatPercent', () => {
  it('never multiplies a rate the server already expressed as 0-100', () => {
    // The server owns every rate. A browser that also multiplied would
    // produce 2000%.
    expect(formatPercent(20)).toBe('20%')
    expect(formatPercent(36.44)).toBe('36.4%')
    expect(formatPercent(0)).toBe('0%')
    // 100 stays 100, not 10000.
    expect(formatPercent(100)).toBe('100%')
  })
})

describe('the mounted call log', () => {
  it("renders call times in the tenant's zone and durations as minutes", async () => {
    window.location.hash = '#/calls'
    installFetch({
      ...sessionRoutes(makeMe()),
      'GET /api/analytics/overview': { body: EMPTY_OVERVIEW },
      'GET /api/tenants/': {
        body: {
          calls: [{
            id: 'call-1', from: '+15551234567', to: '+15551230000',
            direction: 'inbound', started_at: '2026-08-14T18:30:00Z',
            duration: 1320, status: 'completed', booked: true,
            escalated: false, has_recording: false, intent: null,
          }],
          total: 1, limit: 25, offset: 0,
        },
      },
    })

    render(<App />)

    // 14:30 = America/New_York, the tenant's zone in the fixture.
    expect(await screen.findByRole('button', { name: /14:30/ }))
      .toBeInTheDocument()
    expect(screen.getByText('22m')).toBeInTheDocument()
    expect(screen.queryByText('1320s')).not.toBeInTheDocument()
  })
})
