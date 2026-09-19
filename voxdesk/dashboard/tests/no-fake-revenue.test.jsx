/**
 * No invented revenue in the active dashboard.
 *
 * The audit's F1, and the single most misleading thing the old UI did:
 *
 *     estimated_value_usd = booked * 150     # app/api/routes.py
 *     { label: 'Est. revenue captured', ... } // StatCards.jsx
 *
 * $150 is a constant. It is not the tenant's average job value, not derived
 * from any appointment, lead or invoice, and not configurable. These tests
 * assert it is gone from what a user can see — and that nothing invented a
 * replacement figure in its place.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import App from '../src/App'
import StatCards from '../src/components/StatCards'
import { EMPTY_OVERVIEW, installFetch, makeMe, sessionRoutes } from './harness'

describe('the mounted overview', () => {
  it('never renders an estimated-revenue figure', async () => {
    installFetch({
      ...sessionRoutes(),
      // A tenant with real activity, so the page is not empty by accident.
      'GET /api/analytics/overview': {
        body: {
          ...EMPTY_OVERVIEW,
          calls: {
            total: 40, answered: 33, missed: 5, failed: 2, transferred: 4,
            average_duration_seconds: 210,
          },
          conversion: { booked: 12, booking_rate: 36.4, qualified_leads: 18 },
          operations: { appointments: 12, cancellations: 1, no_shows: 2 },
        },
      },
    })

    render(<App />)
    await screen.findByRole('heading', { name: /overview/i, level: 1 })

    // No fabricated figure anywhere on the page.
    expect(screen.queryByText(/est\.? revenue/i)).toBeNull()
    // 12 booked x $150 would have been $1,800.
    expect(screen.queryByText(/\$1,?800/)).toBeNull()
    expect(document.body.textContent).not.toMatch(/\$\s?\d/)
  })

  it('states plainly that revenue is unavailable instead of inventing one', async () => {
    installFetch({
      ...sessionRoutes(),
      'GET /api/analytics/overview': {
        body: {
          ...EMPTY_OVERVIEW,
          // The body short-circuits to an empty state at zero calls, so the
          // tenant must have real volume for the cards to render at all.
          calls: {
            total: 40, answered: 33, missed: 5, failed: 2, transferred: 4,
            average_duration_seconds: 210,
          },
          conversion: { booked: 12, booking_rate: 36.4, qualified_leads: 18 },
          operations: { appointments: 12, cancellations: 1, no_shows: 2 },
        },
      },
    })

    render(<App />)
    // The heading renders while the section is still a skeleton, so wait for
    // the card itself rather than the page title.
    const label = await screen.findByText(/revenue captured/i)

    // The honest replacement: a labelled card that says the number is not
    // available and why, rather than a silent removal or a made-up value.
    const card = label.closest('.card')
    expect(card).toHaveTextContent(/not available/i)
    expect(card).toHaveTextContent(/does not know what an appointment is worth/i)
    expect(card.textContent).not.toMatch(/\$\s?\d/)
  })

  it('does not call the legacy /stats endpoint that computes the figure', async () => {
    const { calls } = installFetch({
      ...sessionRoutes(),
      'GET /api/analytics/overview': { body: EMPTY_OVERVIEW },
    })

    render(<App />)
    await screen.findByRole('heading', { name: /overview/i, level: 1 })
    await waitFor(() =>
      expect(calls.some((c) => c.path.includes('/analytics/overview'))).toBe(true)
    )

    expect(calls.some((c) => c.path.includes('/stats'))).toBe(false)
  })
})

describe('the legacy StatCards component', () => {
  it('no longer displays estimated_value_usd even if the API returns it', () => {
    render(
      <StatCards
        stats={{
          calls: 40, booked: 12, booking_rate: 0.3, minutes: 88,
          // Still present in the response for API compatibility.
          estimated_value_usd: 1800,
        }}
      />
    )

    expect(screen.queryByText(/est\.? revenue/i)).toBeNull()
    expect(screen.queryByText('$1800')).toBeNull()
    // The honest figures survive.
    expect(screen.getByText('Calls answered')).toBeInTheDocument()
    expect(screen.getByText('12')).toBeInTheDocument()
  })
})

describe('the dashboard source', () => {
  it('reads estimated_value_usd nowhere', async () => {
    const { readFileSync, readdirSync, statSync } = await import('node:fs')
    const { join, resolve } = await import('node:path')

    const offenders = []
    const walk = (dir) => {
      for (const entry of readdirSync(dir)) {
        const full = join(dir, entry)
        if (statSync(full).isDirectory()) { walk(full); continue }
        if (!/\.jsx?$/.test(entry)) continue
        const code = readFileSync(full, 'utf8')
          .replace(/\/\*[\s\S]*?\*\//g, '')
          .replace(/^\s*\/\/.*$/gm, '')
        if (code.includes('estimated_value_usd')) offenders.push(full)
      }
    }
    walk(resolve(process.cwd(), 'src'))

    expect(offenders).toEqual([])
  })
})