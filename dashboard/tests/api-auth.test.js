/**
 * API client auth behaviour.
 *
 * Findings F14 and F15 said the token handling was already right and should be
 * carried forward. "Carried forward" is only meaningful if something asserts
 * it, so this pins the properties: the access token never reaches storage, a
 * 401 rotates exactly once, and a failed rotation logs out rather than looping.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { installFetch } from './harness'

// A fresh module per test: `accessToken` is module-level state by design.
async function freshApi() {
  vi.resetModules()
  return import('../src/lib/api')
}

describe('token storage', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
  })

  it('never puts the access token in localStorage or sessionStorage', async () => {
    const api = await freshApi()
    installFetch({
      'POST /auth/login': {
        body: { access_token: 'secret-token', user: { id: 'u-1' } },
      },
    })

    await api.login('owner@example.com', 'correct horse')

    expect(api.isAuthenticated()).toBe(true)
    expect(JSON.stringify(localStorage)).not.toContain('secret-token')
    expect(JSON.stringify(sessionStorage)).not.toContain('secret-token')
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it('sends the bearer token on an authenticated request', async () => {
    const api = await freshApi()
    const { calls } = installFetch({
      'POST /auth/login': { body: { access_token: 'tok-1', user: {} } },
      'GET /api/billing': { body: { plan_code: 'pro' } },
    })

    await api.login('a@b.c', 'pw')
    await api.getBilling()

    const billing = calls.find((c) => c.path === '/api/billing')
    expect(billing.options.headers.Authorization).toBe('Bearer tok-1')
  })
})

describe('401 handling', () => {
  it('rotates the token once and replays the request', async () => {
    const api = await freshApi()
    let attempts = 0

    installFetch({
      'POST /auth/login': { body: { access_token: 'stale', user: {} } },
      'POST /auth/refresh': { body: { access_token: 'fresh' } },
      'GET /api/billing': ({ options }) => {
        attempts += 1
        // First call presents the stale token and is rejected; the replay
        // must present the rotated one.
        if (options.headers.Authorization === 'Bearer stale') {
          return { status: 401, body: { detail: 'expired' } }
        }
        return { body: { plan_code: 'pro' } }
      },
    })

    await api.login('a@b.c', 'pw')
    const result = await api.getBilling()

    expect(result).toEqual({ plan_code: 'pro' })
    expect(attempts).toBe(2)
  })

  it('logs out and does not loop when the refresh also fails', async () => {
    const api = await freshApi()
    const onUnauthorized = vi.fn()
    let refreshes = 0

    installFetch({
      'POST /auth/login': { body: { access_token: 'stale', user: {} } },
      'POST /auth/refresh': () => {
        refreshes += 1
        return { status: 401, body: { detail: 'gone' } }
      },
      'GET /api/billing': { status: 401, body: { detail: 'expired' } },
    })

    await api.login('a@b.c', 'pw')
    api.setUnauthorizedHandler(onUnauthorized)

    await expect(api.getBilling()).rejects.toMatchObject({ status: 401 })

    expect(onUnauthorized).toHaveBeenCalledTimes(1)
    expect(api.isAuthenticated()).toBe(false)
    // Exactly one rotation attempt. More than one is the loop F15 forbids.
    expect(refreshes).toBe(1)
  })

  it('clears the local session even if the logout request fails', async () => {
    const api = await freshApi()
    const onUnauthorized = vi.fn()

    installFetch({
      'POST /auth/login': { body: { access_token: 'tok', user: {} } },
      'POST /auth/logout': { status: 500, body: { detail: 'boom' } },
      'POST /auth/refresh': { status: 401, body: {} },
    })

    await api.login('a@b.c', 'pw')
    api.setUnauthorizedHandler(onUnauthorized)
    await api.logout()

    expect(api.isAuthenticated()).toBe(false)
    expect(onUnauthorized).toHaveBeenCalled()
  })
})

describe('error surfacing', () => {
  it('does not leak a raw backend exception to the user', async () => {
    const api = await freshApi()
    installFetch({
      'GET /api/billing': {
        status: 500,
        body: { detail: { traceback: 'File "app/billing/service.py", line 12' } },
      },
    })

    await expect(api.getBilling()).rejects.toMatchObject({
      status: 500,
      message: 'Something went wrong. Please try again.',
    })
  })

  it('reports a network failure as status 0 rather than undefined', async () => {
    const api = await freshApi()
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))

    await expect(api.getBilling()).rejects.toMatchObject({
      status: 0,
      message: expect.stringMatching(/could not reach the server/i),
    })
  })

  it('omits empty query parameters instead of sending them blank', async () => {
    const api = await freshApi()
    const { calls } = installFetch({ 'GET /api/tenants/': { body: {} } })

    await api.listCalls('t-1', { limit: 25, status: '', search: undefined })

    expect(calls[0].path).toBe('/api/tenants/t-1/calls?limit=25')
  })
})