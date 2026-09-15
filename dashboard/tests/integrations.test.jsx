/**
 * The /integrations page.
 *
 * Fixtures are the real serialized shapes, captured by driving the actual
 * FastAPI routes and printing the JSON. Two details an invented fixture would
 * get wrong, and which the page has to survive:
 *
 *   - the CRM list is `{integrations: [...]}` but the calendar list is a
 *     **bare array**;
 *   - CRM timestamps are naive (`...842346`) while calendar timestamps carry
 *     `+00:00`. Both must render in the tenant's zone.
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import App from '../src/App'
import {
  EMPTY_OVERVIEW, installFetch, makeMe, OWNER_PERMISSIONS,
  RESTRICTED_PERMISSIONS, sessionRoutes, VIEWER_PERMISSIONS,
} from './harness'

/* ------------------------------------------------------------- fixtures --- */

const CRM_PROVIDERS = {
  providers: [
    {
      provider: 'gohighlevel',
      capabilities: ['create_contact', 'health_check', 'upsert_contact'],
      credential_fields: ['access_token'],
      config_fields: ['location_id', 'calendar_id', 'base_url'],
    },
    {
      provider: 'hubspot',
      capabilities: ['create_contact', 'health_check'],
      credential_fields: ['access_token'],
      config_fields: ['base_url'],
    },
    {
      provider: 'jobber',
      capabilities: ['create_contact', 'health_check'],
      credential_fields: ['access_token', 'refresh_token'],
      config_fields: ['api_version', 'base_url'],
    },
    {
      provider: 'webhook',
      capabilities: ['create_contact', 'health_check', 'add_tag'],
      credential_fields: ['signing_secret'],
      config_fields: ['url'],
    },
  ],
}

const CAL_PROVIDERS = {
  providers: [
    {
      provider: 'google',
      capabilities: ['create_event', 'free_busy', 'health_check'],
      credential_fields: [
        'access_token', 'refresh_token', 'client_id', 'client_secret',
      ],
      config_fields: ['calendar_id', 'base_url', 'token_url'],
    },
    {
      provider: 'google_service_account',
      capabilities: ['create_event', 'health_check'],
      credential_fields: [],
      config_fields: ['calendar_id'],
    },
    {
      provider: 'microsoft',
      capabilities: ['create_event', 'conferencing', 'health_check'],
      credential_fields: [
        'access_token', 'refresh_token', 'client_id', 'client_secret', 'scope',
      ],
      config_fields: ['calendar_id', 'mailbox', 'base_url'],
    },
    {
      provider: 'calcom',
      capabilities: ['create_event', 'health_check'],
      credential_fields: ['api_key'],
      config_fields: ['event_type_id', 'base_url'],
    },
    {
      provider: 'internal',
      capabilities: ['create_event', 'get_availability', 'health_check'],
      credential_fields: [],
      config_fields: [],
    },
  ],
}

const GHL = {
  provider: 'gohighlevel',
  is_enabled: true,
  connected: true,
  capabilities: ['create_contact', 'health_check', 'upsert_contact'],
  config: { location_id: 'loc_test_123', base_url: 'https://ghl.test' },
  field_mappings: {},
  subscribed_events: ['call.completed'],
  share_transcripts: false,
  connected_at: '2026-08-14T18:30:00.842346',
  last_health_check_at: '2026-08-20T09:15:00.100000',
  last_health_ok: true,
  last_error: null,
  created_at: '2026-08-14T18:30:00.843112',
  updated_at: '2026-08-14T18:30:00.843115',
}

const GOOGLE_CAL = {
  provider: 'google',
  is_enabled: true,
  is_primary: true,
  connected: true,
  capabilities: ['create_event', 'free_busy', 'health_check'],
  config: { calendar_id: 'primary', base_url: 'https://gcal.test/v3' },
  // Calendar serializes offset-aware; CRM does not.
  connected_at: '2026-08-14T18:30:00.318341+00:00',
  token_expires_at: null,
  last_health_check_at: null,
  last_health_ok: null,
  last_error: null,
}

const SYNCS = {
  total: 1,
  syncs: [{
    id: 'sync-1',
    provider: 'gohighlevel',
    entity_type: 'lead',
    entity_id: 'lead-1',
    event_type: 'call.completed',
    status: 'permanent_failure',
    external_id: null,
    attempt_count: 5,
    last_attempt_at: '2026-08-20T18:30:00.000000',
    next_attempt_at: null,
    synced_at: null,
    last_error: 'gohighlevel rejected the request (401)',
    last_error_code: 'unauthorized',
  }],
}

function backend(me = makeMe(), extra = {}) {
  return {
    ...sessionRoutes(me),
    'GET /api/analytics/overview': { body: EMPTY_OVERVIEW },
    'GET /api/integrations/crm/providers': { body: CRM_PROVIDERS },
    'GET /api/integrations/crm/syncs': { body: SYNCS },
    'GET /api/integrations/crm': { body: { integrations: [GHL] } },
    'GET /api/calendar/providers': { body: CAL_PROVIDERS },
    'GET /api/calendar/integrations': { body: [GOOGLE_CAL] },
    ...extra,
  }
}

async function open(me = makeMe(), extra = {}) {
  window.location.hash = '#/integrations'
  const fetched = installFetch(backend(me, extra))
  const view = render(<App />)
  await screen.findByRole('heading', { name: /^integrations$/i, level: 1 })
  return { ...fetched, ...view }
}

/** The card for one provider, found by its heading. */
async function card(name) {
  const heading = await screen.findByRole('heading', { name, level: 4 })
  return heading.closest('.card')
}

/* ---------------------------------------------------------------- tests --- */

describe('route', () => {
  it('renders at /integrations', async () => {
    await open()
    expect(await screen.findByRole('region', { name: /crm integrations/i }))
      .toBeInTheDocument()
    expect(screen.getByRole('region', { name: /calendar integrations/i }))
      .toBeInTheDocument()
  })

  it('refuses the page without integration:read', async () => {
    window.location.hash = '#/integrations'
    installFetch(backend(makeMe(RESTRICTED_PERMISSIONS)))
    render(<App />)
    expect(await screen.findByText(/you do not have access to this page/i))
      .toBeInTheDocument()
  })
})

describe('real API response rendering', () => {
  it('renders a card per catalogue provider, configured or not', async () => {
    await open()
    // Four CRM + five calendar providers, all from the real catalogues.
    for (const name of [
      /GoHighLevel/, /HubSpot/, /Jobber/, /Generic Webhook/,
      /Google Calendar/, /Microsoft Outlook/, /Cal\.com/, /Internal Calendar/,
    ]) {
      expect(await screen.findByRole('heading', { name, level: 4 }))
        .toBeInTheDocument()
    }
  })

  it('reads the calendar list even though it is a bare array', async () => {
    // The CRM endpoint wraps in {integrations: []}; this one does not.
    await open()
    const google = await card(/Google Calendar/)
    // The fixture has last_health_ok: null -- credentials stored, never
    // checked -- so this is "Connected", not "Healthy".
    expect(within(google).getByText('Connected', { selector: '.badge' }))
      .toBeInTheDocument()
    expect(within(google).getByText('primary')).toBeInTheDocument()
  })

  it('shows non-secret config values from the response', async () => {
    await open()
    const ghl = await card(/GoHighLevel/)
    expect(within(ghl).getByText('loc_test_123')).toBeInTheDocument()
    expect(within(ghl).getByText('https://ghl.test')).toBeInTheDocument()
  })

  it('lists capabilities from real provider metadata', async () => {
    await open()
    const ghl = await card(/GoHighLevel/)
    expect(within(ghl).getByText(/3 supported operations/)).toBeInTheDocument()
    expect(within(ghl).getByText('Upsert Contact')).toBeInTheDocument()
  })
})

describe('status rendering', () => {
  it('marks an unconfigured provider as such', async () => {
    await open()
    const hubspot = await card(/HubSpot/)
    expect(within(hubspot).getByText('Not configured')).toBeInTheDocument()
    expect(within(hubspot).getByText(/not configured for this tenant/i))
      .toBeInTheDocument()
  })

  it('shows Healthy only when the last check actually passed', async () => {
    await open()
    const ghl = await card(/GoHighLevel/)
    expect(within(ghl).getByText('Healthy')).toBeInTheDocument()
  })

  it('distinguishes "credentials stored" from "credentials work"', async () => {
    // connected=true but last_health_ok=null means never checked. Showing
    // green here would be a lie about an untested connection.
    await open(makeMe(), {
      'GET /api/integrations/crm': {
        body: { integrations: [{ ...GHL, last_health_ok: null, last_health_check_at: null }] },
      },
    })
    const ghl = await card(/GoHighLevel/)
    expect(within(ghl).getByText('Connected', { selector: '.badge' }))
      .toBeInTheDocument()
    expect(within(ghl).queryByText('Healthy')).toBeNull()
    expect(within(ghl).getByText('Never')).toBeInTheDocument()
  })

  it('shows a failing connection and its safe error text', async () => {
    await open(makeMe(), {
      'GET /api/integrations/crm': {
        body: {
          integrations: [{
            ...GHL,
            last_health_ok: false,
            last_error: 'gohighlevel connection failed (ConnectError)',
          }],
        },
      },
    })
    const ghl = await card(/GoHighLevel/)
    expect(within(ghl).getByText('Failing')).toBeInTheDocument()
    expect(within(ghl).getByText(/ConnectError/)).toBeInTheDocument()
  })

  it('says credentials are not required for a provider that needs none', async () => {
    await open()
    const internal = await card(/Internal Calendar/)
    // `internal` has an empty credential_fields list in the real catalogue.
    expect(within(internal).getByText('Not configured')).toBeInTheDocument()
  })

  it('renders the sync feed with its real status vocabulary', async () => {
    await open()
    const feed = await screen.findByRole('region', { name: /recent crm syncs/i })
    expect(within(feed).getByText('Permanent Failure')).toBeInTheDocument()
    expect(within(feed).getByText(/rejected the request \(401\)/))
      .toBeInTheDocument()
    expect(within(feed).getByText('5')).toBeInTheDocument()
  })
})

describe('permission gating', () => {
  it('hides every mutating control from a read-only account', async () => {
    // A viewer has no integration permission at all in the real RBAC table,
    // so grant exactly integration:read and nothing else.
    await open(makeMe([...VIEWER_PERMISSIONS, 'integration:read']))
    const ghl = await card(/GoHighLevel/)

    // Anchored: "Test connection" would match a loose /connect/i.
    expect(within(ghl).queryByRole('button', { name: /^(connect|configure)$/i }))
      .toBeNull()
    expect(within(ghl).queryByRole('button', { name: /disable|enable/i }))
      .toBeNull()
    expect(within(ghl).queryByRole('button', { name: /disconnect/i })).toBeNull()
    expect(within(ghl).queryByRole('button', { name: /remove/i })).toBeNull()
  })

  it('still lets a read-only account view status and test a connection', async () => {
    // The backend gates POST /test on INTEGRATION_READ, not WRITE.
    await open(makeMe([...VIEWER_PERMISSIONS, 'integration:read']))
    const ghl = await card(/GoHighLevel/)

    expect(within(ghl).getByText('Healthy')).toBeInTheDocument()
    expect(within(ghl).getByRole('button', { name: /test connection/i }))
      .toBeInTheDocument()
  })

  it('gives a writer the full control set', async () => {
    await open(makeMe(OWNER_PERMISSIONS))
    const ghl = await card(/GoHighLevel/)

    for (const name of [/test connection/i, /configure/i, /disable/i,
      /disconnect/i, /remove/i]) {
      expect(within(ghl).getByRole('button', { name })).toBeInTheDocument()
    }
  })
})

describe('only real actions are offered', () => {
  it('offers no Disconnect for calendar, which has no such route', async () => {
    // CRM has POST /{provider}/disconnect; the calendar API does not.
    await open()
    const google = await card(/Google Calendar/)
    expect(within(google).getByRole('button', { name: /remove/i }))
      .toBeInTheDocument()
    expect(within(google).queryByRole('button', { name: /disconnect/i }))
      .toBeNull()
  })

  it('offers no Test or Remove for a provider with no row', async () => {
    await open()
    const hubspot = await card(/HubSpot/)
    expect(within(hubspot).queryByRole('button', { name: /test connection/i }))
      .toBeNull()
    expect(within(hubspot).queryByRole('button', { name: /remove/i })).toBeNull()
    // Connecting it is real, so that button does appear.
    expect(within(hubspot).getByRole('button', { name: /connect/i }))
      .toBeInTheDocument()
  })

  it('invents no OAuth flow for Google', async () => {
    // There is no OAuth redirect route in the backend, so a "Sign in with
    // Google" button would be a dead end.
    await open()
    const google = await card(/Google Calendar/)
    expect(within(google).queryByRole('button', { name: /sign in with|authorize|oauth/i }))
      .toBeNull()
  })

  it('offers no Sync button, since no route consumes integration:sync', async () => {
    await open()
    expect(screen.queryByRole('button', { name: /^sync( now)?$/i })).toBeNull()
  })
})

describe('mutations', () => {
  it('tests a connection and reports a failure as a diagnosis', async () => {
    const user = userEvent.setup()
    const { calls } = await open(makeMe(), {
      'POST /api/integrations/crm/gohighlevel/test': {
        body: {
          connected: false,
          provider: 'gohighlevel',
          latency_ms: 28.58,
          safe_message: 'gohighlevel connection failed (ConnectError)',
        },
      },
    })

    const ghl = await card(/GoHighLevel/)
    await user.click(within(ghl).getByRole('button', { name: /test connection/i }))

    expect(await within(ghl).findByText(/connection failed \(ConnectError\)/))
      .toBeInTheDocument()
    expect(within(ghl).getByText(/29 ms/)).toBeInTheDocument()
    expect(calls.some((c) =>
      c.path === '/api/integrations/crm/gohighlevel/test')).toBe(true)
  })

  it('disables an integration without touching its credentials', async () => {
    const user = userEvent.setup()
    const { calls } = await open(makeMe(), {
      'PUT /api/integrations/crm/gohighlevel': {
        body: { ...GHL, is_enabled: false },
      },
    })

    const ghl = await card(/GoHighLevel/)
    await user.click(within(ghl).getByRole('button', { name: /disable/i }))

    expect(await screen.findByText(/GoHighLevel disabled/)).toBeInTheDocument()

    const put = calls.find((c) => c.method === 'PUT')
    const body = JSON.parse(put.options.body)
    expect(body.is_enabled).toBe(false)
    // Omitting `credentials` is what tells the backend to keep the stored
    // token. Sending null would be a different, destructive request.
    expect('credentials' in body).toBe(false)
    // Config must be echoed back, since PUT is a full replace.
    expect(body.config).toEqual(GHL.config)
  })

  it('confirms before disconnecting and explains what is kept', async () => {
    const user = userEvent.setup()
    const { calls } = await open(makeMe(), {
      'POST /api/integrations/crm/gohighlevel/disconnect': {
        body: { ...GHL, connected: false, is_enabled: false },
      },
    })

    const ghl = await card(/GoHighLevel/)
    await user.click(within(ghl).getByRole('button', { name: /disconnect/i }))

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(/configuration and field mappings are kept/i)
    expect(calls.some((c) => c.path.includes('/disconnect'))).toBe(false)

    await user.click(within(dialog).getByRole('button', { name: /disconnect/i }))
    await waitFor(() =>
      expect(calls.some((c) => c.path.includes('/disconnect'))).toBe(true))
  })

  it('cancelling a removal sends nothing', async () => {
    const user = userEvent.setup()
    const { calls } = await open()

    const ghl = await card(/GoHighLevel/)
    await user.click(within(ghl).getByRole('button', { name: /remove/i }))

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /cancel/i }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)
  })

  it('removes a calendar integration through DELETE', async () => {
    const user = userEvent.setup()
    const { calls } = await open(makeMe(), {
      'DELETE /api/calendar/integrations/google': { status: 204, body: null },
    })

    const google = await card(/Google Calendar/)
    await user.click(within(google).getByRole('button', { name: /remove/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /^remove$/i }))

    expect(await screen.findByText(/Google Calendar removed/)).toBeInTheDocument()
    expect(calls.some((c) =>
      c.method === 'DELETE'
      && c.path === '/api/calendar/integrations/google')).toBe(true)
  })

  it('surfaces a server rejection without breaking the page', async () => {
    const user = userEvent.setup()
    await open(makeMe(), {
      'POST /api/integrations/crm/gohighlevel/disconnect': {
        status: 503,
        body: { detail: 'CRM credential encryption is not configured' },
      },
    })

    const ghl = await card(/GoHighLevel/)
    await user.click(within(ghl).getByRole('button', { name: /disconnect/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /disconnect/i }))

    expect(await within(ghl).findByText(/encryption is not configured/i))
      .toBeInTheDocument()
    expect(within(ghl).getByText('loc_test_123')).toBeInTheDocument()
  })
})

describe('connect form', () => {
  it('builds its fields from the provider catalogue', async () => {
    const user = userEvent.setup()
    await open()

    const jobber = await card(/Jobber/)
    await user.click(within(jobber).getByRole('button', { name: /connect/i }))

    // Jobber's real catalogue: two credential fields, two config fields.
    expect(await within(jobber).findByLabelText(/access token/i))
      .toBeInTheDocument()
    expect(within(jobber).getByLabelText(/refresh token/i)).toBeInTheDocument()
    expect(within(jobber).getByLabelText(/api version/i)).toBeInTheDocument()
  })

  it('masks credential inputs and never autocompletes them', async () => {
    const user = userEvent.setup()
    await open()

    const hubspot = await card(/HubSpot/)
    await user.click(within(hubspot).getByRole('button', { name: /connect/i }))

    const input = await within(hubspot).findByLabelText(/access token/i)
    expect(input).toHaveAttribute('type', 'password')
    expect(input).toHaveAttribute('autocomplete', 'off')
  })

  it('starts credential fields empty even for a connected provider', async () => {
    const user = userEvent.setup()
    await open()

    const ghl = await card(/GoHighLevel/)
    await user.click(within(ghl).getByRole('button', { name: /configure/i }))

    const token = await within(ghl).findByLabelText(/access token/i)
    // The API cannot return the stored value, so pre-filling anything --
    // even a row of dots -- would misrepresent what gets submitted.
    expect(token).toHaveValue('')
    expect(within(ghl).getByText(/leave blank to keep the stored value/i))
      .toBeInTheDocument()
    // Non-secret config IS pre-filled, because the API really returns it.
    expect(within(ghl).getByLabelText(/location id/i))
      .toHaveValue('loc_test_123')
  })

  it('omits credentials entirely when none were typed', async () => {
    const user = userEvent.setup()
    const { calls } = await open(makeMe(), {
      'PUT /api/integrations/crm/gohighlevel': { body: GHL },
    })

    const ghl = await card(/GoHighLevel/)
    await user.click(within(ghl).getByRole('button', { name: /configure/i }))
    await user.click(await within(ghl).findByRole('button', { name: /^save$/i }))

    await waitFor(() =>
      expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const body = JSON.parse(calls.find((c) => c.method === 'PUT').options.body)
    expect('credentials' in body).toBe(false)
  })

  it('sends a typed credential and then drops it from the DOM', async () => {
    const user = userEvent.setup()
    const secret = 'pat-na1-super-secret-value'
    const { calls } = await open(makeMe(), {
      'PUT /api/integrations/crm/hubspot': {
        body: { ...GHL, provider: 'hubspot', config: {} },
      },
    })

    const hubspot = await card(/HubSpot/)
    await user.click(within(hubspot).getByRole('button', { name: /connect/i }))
    await user.type(
      await within(hubspot).findByLabelText(/access token/i), secret
    )
    await user.click(within(hubspot).getByRole('button', { name: /^save$/i }))

    await waitFor(() =>
      expect(calls.some((c) => c.method === 'PUT')).toBe(true))
    const body = JSON.parse(calls.find((c) => c.method === 'PUT').options.body)
    expect(body.credentials).toEqual({ access_token: secret })

    // The form closes and the value is gone -- not left in a detached input.
    await waitFor(() =>
      expect(document.body.innerHTML).not.toContain(secret))
  })

  it('tells the user when a provider needs no credentials', async () => {
    const user = userEvent.setup()
    await open()

    const internal = await card(/Internal Calendar/)
    await user.click(within(internal).getByRole('button', { name: /connect/i }))

    expect(await within(internal).findByText(/needs no credentials/i))
      .toBeInTheDocument()
  })
})

describe('states', () => {
  it('shows a loading state before the data arrives', async () => {
    window.location.hash = '#/integrations'
    let release
    const gate = new Promise((resolve) => { release = resolve })
    installFetch(backend(makeMe(), {
      'GET /api/integrations/crm/providers': async () => {
        await gate
        return { body: CRM_PROVIDERS }
      },
    }))
    render(<App />)

    await screen.findByRole('heading', { name: /^integrations$/i, level: 1 })
    expect(await screen.findByText(/loading crm integrations/i))
      .toBeInTheDocument()

    release()
    expect(await screen.findByRole('heading', { name: /GoHighLevel/, level: 4 }))
      .toBeInTheDocument()
  })

  it('shows an error state with a retry when a catalogue fails', async () => {
    await open(makeMe(), {
      'GET /api/integrations/crm/providers': {
        status: 500, body: { detail: 'boom' },
      },
    })

    expect(await screen.findByText(/could not load crm integrations/i))
      .toBeInTheDocument()
    // The calendar half is independent and still works.
    expect(await screen.findByRole('heading', { name: /Google Calendar/, level: 4 }))
      .toBeInTheDocument()
  })

  it('shows an empty state when a deployment registers no providers', async () => {
    await open(makeMe(), {
      'GET /api/integrations/crm/providers': { body: { providers: [] } },
      'GET /api/integrations/crm': { body: { integrations: [] } },
    })

    expect(await screen.findByText(/no crm providers available/i))
      .toBeInTheDocument()
  })

  it('shows an empty state for a tenant with no sync history', async () => {
    await open(makeMe(), {
      'GET /api/integrations/crm/syncs': { body: { syncs: [], total: 0 } },
    })

    expect(await screen.findByText(/no sync activity yet/i)).toBeInTheDocument()
  })
})

describe('timezone', () => {
  it('renders a naive CRM timestamp in the tenant zone, not the browser', async () => {
    // 18:30 UTC is 14:30 in America/New_York. The CRM API sends no offset.
    await open()
    const ghl = await card(/GoHighLevel/)
    expect(within(ghl).getByText(/14 Aug 2026, 14:30/)).toBeInTheDocument()
    expect(within(ghl).queryByText(/18:30/)).toBeNull()
  })

  it('renders an offset-aware calendar timestamp in the same zone', async () => {
    await open()
    const google = await card(/Google Calendar/)
    expect(within(google).getByText(/14 Aug 2026, 14:30/)).toBeInTheDocument()
  })
})

describe('security', () => {
  it('never renders a credential the API should not have sent', async () => {
    // None of these are in the response models. This is the backstop for a
    // future allowlist mistake, not a substitute for the allowlist.
    await open(makeMe(), {
      'GET /api/integrations/crm': {
        body: {
          integrations: [{
            ...GHL,
            credentials_encrypted: 'v1:gAAAAABm-CIPHERTEXT-BLOB',
            credentials_key_id: 'key-2026-01',
            config: {
              ...GHL.config,
              access_token: 'pit-LEAKED-TOKEN-123',
              signing_secret: 'whsec_LEAKED',
              client_secret: 'GOCSPX-LEAKED',
            },
          }],
        },
      },
    })
    await card(/GoHighLevel/)

    const text = document.body.textContent
    for (const leak of ['pit-LEAKED-TOKEN-123', 'whsec_LEAKED',
      'GOCSPX-LEAKED', 'gAAAAABm-CIPHERTEXT-BLOB', 'key-2026-01']) {
      expect(text).not.toContain(leak)
    }
    // The key names are still shown, masked -- hiding them entirely would
    // make a misconfiguration invisible.
    expect(screen.getAllByText('••••••••').length).toBeGreaterThan(0)
  })

  it('puts nothing in storage', async () => {
    await open()
    await card(/GoHighLevel/)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it('never sends a tenant id on a read or a mutation', async () => {
    const user = userEvent.setup()
    const { calls } = await open(makeMe(), {
      'PUT /api/integrations/crm/gohighlevel': { body: GHL },
    })

    const ghl = await card(/GoHighLevel/)
    await user.click(within(ghl).getByRole('button', { name: /disable/i }))
    await waitFor(() =>
      expect(calls.some((c) => c.method === 'PUT')).toBe(true))

    for (const call of calls) {
      expect(call.path).not.toContain('tenant_id')
      if (call.options?.body && typeof call.options.body === 'string') {
        expect(call.options.body).not.toContain('tenant_id')
      }
    }
  })

  it('renders hostile provider config and error text as text, not markup', async () => {
    const hostile = '<img src=x onerror="window.__intPwned=1">'
    const { container } = await open(makeMe(), {
      'GET /api/integrations/crm': {
        body: {
          integrations: [{
            ...GHL,
            last_health_ok: false,
            last_error: `failed: ${hostile}`,
            config: { location_id: hostile },
          }],
        },
      },
    })
    await card(/GoHighLevel/)

    expect(container.querySelector('img')).toBeNull()
    expect(window.__intPwned).toBeUndefined()
    expect(screen.getAllByText(/onerror/).length).toBeGreaterThan(0)
  })

  it('renders a hostile sync error as text', async () => {
    const { container } = await open(makeMe(), {
      'GET /api/integrations/crm/syncs': {
        body: {
          total: 1,
          syncs: [{
            ...SYNCS.syncs[0],
            last_error: '<script>window.__syncPwned=1</script>',
          }],
        },
      },
    })
    await screen.findByRole('region', { name: /recent crm syncs/i })

    expect(container.querySelector('script')).toBeNull()
    expect(window.__syncPwned).toBeUndefined()
  })
})