/**
 * Dashboard boot.
 *
 * The audit's blocker B1 was that `App.jsx` imported `getCalls` and `getStats`
 * from `lib/api.js`, which does not export them — the app could not start at
 * all, and nothing caught it because there were no frontend tests. These are
 * the tests that would have caught it.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import App from '../src/App'
import {
  EMPTY_OVERVIEW, installFetch, makeMe, sessionRoutes,
} from './harness'

describe('dashboard boot', () => {
  it('mounts the shell after resuming a session from the refresh cookie', async () => {
    installFetch({
      ...sessionRoutes(),
      'GET /api/analytics/overview': { body: EMPTY_OVERVIEW },
    })

    render(<App />)

    // The shell, not the legacy single screen.
    expect(await screen.findByRole('navigation', { name: /main navigation/i }))
      .toBeInTheDocument()
    expect(screen.getByText('Bright Smile Dental')).toBeInTheDocument()
    expect(screen.getByText('owner@example.com')).toBeInTheDocument()
  })

  it('shows the login form when there is no refresh cookie', async () => {
    installFetch({
      'POST /auth/refresh': { status: 401, body: { detail: 'no cookie' } },
    })

    render(<App />)

    expect(await screen.findByLabelText(/email/i)).toBeInTheDocument()
    expect(screen.queryByRole('navigation', { name: /main navigation/i }))
      .not.toBeInTheDocument()
  })

  it('imports nothing from lib/api that lib/api does not export', async () => {
    // A structural regression test for the exact blocker. `App.jsx` importing
    // a missing binding is a build error under a bundler but silently
    // `undefined` under some transforms, so assert it directly.
    const api = await import('../src/lib/api')
    const { readFileSync } = await import('node:fs')
    const { resolve } = await import('node:path')
    // Vitest runs with the dashboard as cwd; `import.meta.url` is an http URL
    // under jsdom and cannot be handed to `fs`.
    const source = readFileSync(resolve(process.cwd(), 'src/App.jsx'), 'utf8')

    const importBlock = source.match(/import \{([^}]+)\} from '\.\/lib\/api'/)
    expect(importBlock, 'App.jsx should import from lib/api').toBeTruthy()

    const named = importBlock[1]
      .split(',')
      .map((entry) => entry.trim())
      .filter(Boolean)

    for (const name of named) {
      expect(api[name], `lib/api must export ${name}`).toBeDefined()
    }
  })

  it('renders the boot state before the session resolves', () => {
    installFetch({
      // Never resolves during this assertion.
      'POST /auth/refresh': () => new Promise(() => {}),
    })

    render(<App />)
    expect(screen.getByRole('status')).toHaveTextContent(/loading/i)
  })
})
