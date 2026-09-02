/**
 * The /agent page.
 *
 * The fixture below is the *real* serialized shape of `AgentConfigOut`, taken
 * from `model_dump_json()` against the Pydantic model rather than invented --
 * including `business_open` as `"09:00:00"`, which is how a `datetime.time`
 * column crosses the wire and is exactly the kind of detail a hand-written
 * fixture gets wrong.
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import App from '../src/App'
import {
  EMPTY_OVERVIEW, installFetch, makeMe, OWNER_PERMISSIONS, sessionRoutes,
  VIEWER_PERMISSIONS,
} from './harness'

/** Exactly what `GET /api/tenants/{id}/agent` returns. */
const AGENT_CONFIG = {
  id: '11111111-1111-1111-1111-111111111111',
  name: 'Bright Smile Dental',
  industry: 'dental',
  twilio_number: '+15551230000',
  agent_name: 'Riya',
  greeting: 'Thanks for calling Bright Smile. How can I help?',
  system_prompt_extra: 'Always confirm insurance before booking.',
  llm_preset: 'natural',
  llm_provider: null,
  llm_model: null,
  temperature: 0.65,
  voice_id: null,
  language: 'en-US',
  humanize: true,
  vad_stop_secs: 0.45,
  speech_speed: 1.0,
  timezone: 'America/New_York',
  business_open: '09:00:00',
  business_close: '17:00:00',
  appointment_minutes: 30,
  escalation_number: '+15559876543',
  notify_sms_number: null,
  record_calls: true,
  recording_disclaimer: 'This call may be recorded for quality purposes.',
  sms_enabled: true,
  whatsapp_enabled: false,
  ivr_enabled: false,
}

const PRESETS = [
  { key: 'fast', brand: 'ChatGPT', provider: 'openai', model: 'gpt-4o-mini',
    latency_ms: 250, notes: '' },
  { key: 'natural', brand: 'Claude', provider: 'anthropic',
    model: 'claude-haiku-4-5', latency_ms: 300, notes: '' },
]

function backend(me = makeMe(), extra = {}) {
  return {
    ...sessionRoutes(me),
    'GET /api/analytics/overview': { body: EMPTY_OVERVIEW },
    'GET /api/llm/presets': { body: PRESETS },
    'GET /api/tenants/': { body: AGENT_CONFIG },
    ...extra,
  }
}

async function openAgent(me = makeMe(), extra = {}) {
  window.location.hash = '#/agent'
  const fetched = installFetch(backend(me, extra))
  const view = render(<App />)
  await screen.findByRole('heading', { name: /^agent$/i, level: 1 })
  return { ...fetched, ...view }
}

describe('route rendering', () => {
  it('renders the agent page at /agent', async () => {
    await openAgent()
    expect(await screen.findByRole('region', { name: /identity/i }))
      .toBeInTheDocument()
  })

  it('requests the config from the tenant-scoped agent endpoint', async () => {
    const { calls } = await openAgent()
    await screen.findByRole('region', { name: /identity/i })

    const request = calls.find((c) => c.path.includes('/agent'))
    expect(request.path).toBe(
      '/api/tenants/11111111-1111-1111-1111-111111111111/agent'
    )
    expect(request.method).toBe('GET')
  })

  it('appears in the sidebar for a viewer, who may read it', async () => {
    await openAgent(makeMe(VIEWER_PERMISSIONS))
    const nav = screen.getByRole('navigation', { name: /main navigation/i })
    expect(within(nav).getByRole('link', { name: /agent/i })).toBeInTheDocument()
  })
})

describe('real API response rendering', () => {
  it('renders values from the response, never invented defaults', async () => {
    await openAgent()

    expect(await screen.findByText('Riya')).toBeInTheDocument()
    expect(screen.getByText(/thanks for calling bright smile/i)).toBeInTheDocument()
    expect(screen.getByText(/always confirm insurance/i)).toBeInTheDocument()
    expect(screen.getByText('+15551230000')).toBeInTheDocument()
    expect(screen.getByText('30 minutes')).toBeInTheDocument()
    // Never the model default.
    expect(screen.queryByText('Alex')).not.toBeInTheDocument()
  })

  it('renders business hours from the time columns', async () => {
    await openAgent()
    const behaviour = await screen.findByRole('region', { name: /call behaviour/i })
    expect(behaviour).toHaveTextContent('09:00')
    expect(behaviour).toHaveTextContent('17:00')
    // The tenant's zone is named, so the hours are not read as the viewer's.
    expect(behaviour).toHaveTextContent('America/New_York')
  })

  it('says a value is not set rather than showing a plausible default', async () => {
    await openAgent(makeMe(), {
      'GET /api/tenants/': {
        body: { ...AGENT_CONFIG, escalation_number: null, agent_name: '' },
      },
    })

    const behaviour = await screen.findByRole('region', { name: /call behaviour/i })
    expect(behaviour).toHaveTextContent(/no transfer number set/i)
  })

  it('seeds the writable controls from the response', async () => {
    await openAgent()
    expect(await screen.findByLabelText(/preset/i)).toHaveValue('natural')
    expect(screen.getByLabelText(/temperature/i)).toHaveValue(0.65)
    expect(screen.getByLabelText(/natural speech/i)).toBeChecked()
  })

  it('keeps an unlisted preset visible instead of snapping to another', async () => {
    await openAgent(makeMe(), {
      'GET /api/tenants/': { body: { ...AGENT_CONFIG, llm_preset: 'legacy-preset' } },
    })
    expect(await screen.findByLabelText(/preset/i)).toHaveValue('legacy-preset')
  })

  it('survives the preset catalogue failing without losing the page', async () => {
    await openAgent(makeMe(), {
      'GET /api/llm/presets': { status: 500, body: { detail: 'boom' } },
    })

    expect(await screen.findByText(/preset catalogue could not be loaded/i))
      .toBeInTheDocument()
    // The configuration itself still rendered.
    expect(screen.getByText('Riya')).toBeInTheDocument()
  })
})

describe('permission gating', () => {
  it('gives an owner working write controls', async () => {
    await openAgent(makeMe(OWNER_PERMISSIONS))
    expect(await screen.findByLabelText(/temperature/i)).toBeEnabled()
    expect(screen.getByRole('button', { name: /save changes/i })).toBeInTheDocument()
  })

  it('disables every control for a read-only account', async () => {
    await openAgent(makeMe(VIEWER_PERMISSIONS))

    expect(await screen.findByText(/read-only access/i)).toBeInTheDocument()
    expect(screen.getByLabelText(/preset/i)).toBeDisabled()
    expect(screen.getByLabelText(/temperature/i)).toBeDisabled()
    expect(screen.getByLabelText(/voice id/i)).toBeDisabled()
    expect(screen.getByLabelText(/speech speed/i)).toBeDisabled()
    expect(screen.getByLabelText(/natural speech/i)).toBeDisabled()
  })

  it('hides the save controls entirely without tenant:update', async () => {
    await openAgent(makeMe(VIEWER_PERMISSIONS))
    await screen.findByText(/read-only access/i)

    expect(screen.queryByRole('button', { name: /save changes/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /discard/i })).toBeNull()
  })

  it('still shows a read-only account the configuration', async () => {
    await openAgent(makeMe(VIEWER_PERMISSIONS))
    // Gating write access must not blank the page.
    expect(await screen.findByText('Riya')).toBeInTheDocument()
    expect(screen.getByText(/thanks for calling bright smile/i)).toBeInTheDocument()
  })
})

describe('loading and error states', () => {
  it('shows a loading state while the config is in flight', async () => {
    window.location.hash = '#/agent'
    installFetch({
      ...sessionRoutes(),
      'GET /api/llm/presets': { body: PRESETS },
      'GET /api/tenants/': () => new Promise(() => {}),
    })
    render(<App />)

    expect(await screen.findByText(/loading the agent configuration/i))
      .toBeInTheDocument()
  })

  it('shows a retryable error state when the request fails', async () => {
    await openAgent(makeMe(), {
      'GET /api/tenants/': { status: 500, body: { detail: 'boom' } },
    })

    expect(await screen.findByText(/could not load the agent configuration/i))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })

  it('explains a 403 rather than offering a pointless retry', async () => {
    await openAgent(makeMe(), {
      'GET /api/tenants/': { status: 403, body: { detail: 'nope' } },
    })

    expect(await screen.findByText(/do not have access/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /try again/i })).toBeNull()
  })
})

describe('saving', () => {
  it('sends only the changed field, and only writable ones', async () => {
    const user = userEvent.setup()
    const { calls } = await openAgent(makeMe(), {
      'PATCH /api/tenants/': { body: { ok: true } },
    })

    const temperature = await screen.findByLabelText(/temperature/i)
    await user.clear(temperature)
    await user.type(temperature, '1.2')
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() => {
      const patch = calls.find((c) => c.method === 'PATCH')
      expect(patch).toBeTruthy()
    })

    const patch = calls.find((c) => c.method === 'PATCH')
    expect(patch.path).toBe(
      '/api/tenants/11111111-1111-1111-1111-111111111111/voice'
    )
    const body = JSON.parse(patch.options.body)
    // Only what changed -- an untouched field must not be resent.
    expect(body).toEqual({ temperature: 1.2 })
    // Never a field the write API does not accept.
    expect(body).not.toHaveProperty('greeting')
    expect(body).not.toHaveProperty('agent_name')
    expect(body).not.toHaveProperty('twilio_number')
  })

  it('confirms success and refetches the server copy', async () => {
    const user = userEvent.setup()
    const { calls } = await openAgent(makeMe(), {
      'PATCH /api/tenants/': { body: { ok: true } },
    })

    await user.click(await screen.findByLabelText(/natural speech/i))
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    expect(await screen.findByText(/agent settings saved/i)).toBeInTheDocument()
    // The server is the source of truth, so the page re-reads it.
    await waitFor(() => {
      const reads = calls.filter(
        (c) => c.method === 'GET' && c.path.includes('/agent')
      )
      expect(reads.length).toBeGreaterThanOrEqual(2)
    })
  })

  it('surfaces a save failure without discarding what was typed', async () => {
    const user = userEvent.setup()
    await openAgent(makeMe(), {
      'PATCH /api/tenants/': {
        status: 422, body: { detail: 'temperature must be between 0 and 2' },
      },
    })

    const temperature = await screen.findByLabelText(/temperature/i)
    await user.clear(temperature)
    await user.type(temperature, '1.9')
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /temperature must be between 0 and 2/i
    )
    // The edit survives, so the operator can correct it.
    expect(screen.getByLabelText(/temperature/i)).toHaveValue(1.9)
  })

  it('cannot save when nothing has changed', async () => {
    await openAgent()
    expect(await screen.findByRole('button', { name: /save changes/i }))
      .toBeDisabled()
    expect(screen.getByText(/no changes/i)).toBeInTheDocument()
  })

  it('discards edits back to the server copy', async () => {
    const user = userEvent.setup()
    await openAgent()

    const temperature = await screen.findByLabelText(/temperature/i)
    await user.clear(temperature)
    await user.type(temperature, '1.5')
    expect(screen.getByText(/unsaved changes/i)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /discard/i }))
    expect(screen.getByLabelText(/temperature/i)).toHaveValue(0.65)
  })
})

describe('security', () => {
  it('renders no credential even if the API is made to return one', async () => {
    // Defence in depth: the response model omits these, but if a future
    // change added one the page must still not print it.
    await openAgent(makeMe(), {
      'GET /api/tenants/': {
        body: {
          ...AGENT_CONFIG,
          crm_api_key: 'sk-live-SHOULD-NEVER-RENDER',
          a2p_brand_sid: 'BN-SECRET-BRAND',
          twilio_auth_token: 'AUTHTOKEN-SECRET',
        },
      },
    })
    await screen.findByText('Riya')

    const body = document.body.textContent
    expect(body).not.toContain('sk-live-SHOULD-NEVER-RENDER')
    expect(body).not.toContain('BN-SECRET-BRAND')
    expect(body).not.toContain('AUTHTOKEN-SECRET')
  })

  it('never sends a credential-shaped field back on save', async () => {
    const user = userEvent.setup()
    const { calls } = await openAgent(makeMe(), {
      'GET /api/tenants/': {
        body: { ...AGENT_CONFIG, crm_api_key: 'sk-live-secret' },
      },
      'PATCH /api/tenants/': { body: { ok: true } },
    })

    await user.click(await screen.findByLabelText(/natural speech/i))
    await user.click(screen.getByRole('button', { name: /save changes/i }))

    await waitFor(() =>
      expect(calls.find((c) => c.method === 'PATCH')).toBeTruthy()
    )
    const patch = calls.find((c) => c.method === 'PATCH')
    expect(patch.options.body).not.toContain('sk-live-secret')
    expect(patch.options.body).not.toContain('crm_api_key')
  })

  it('renders tenant-supplied text as text, not markup', async () => {
    const hostile = '<img src=x onerror="window.__agentPwned=1">'
    const { container } = await openAgent(makeMe(), {
      'GET /api/tenants/': {
        body: {
          ...AGENT_CONFIG,
          greeting: hostile,
          system_prompt_extra: hostile,
          agent_name: hostile,
        },
      },
    })
    await screen.findByRole('region', { name: /personality/i })

    expect(container.querySelector('img')).toBeNull()
    expect(window.__agentPwned).toBeUndefined()
    // Visible as characters.
    expect(screen.getAllByText(/onerror/).length).toBeGreaterThan(0)
  })
})
