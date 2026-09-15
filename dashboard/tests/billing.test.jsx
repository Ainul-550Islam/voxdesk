/**
 * The /billing page.
 *
 * Fixtures are the real serialized shapes, captured by driving the actual
 * FastAPI routes and printing the JSON. Details that matter and that an
 * invented fixture would miss:
 *
 *   - `GET /api/billing` and `/usage` both carry usage, in *different*
 *     shapes (a dict keyed by metric vs. a list of metric objects);
 *   - a plan with no annual price sends `annual_price_cents: null`;
 *   - feature entitlements use `-1` for unlimited;
 *   - an unsupported provider operation is a **501** whose detail is an
 *     object, `{code: 'unsupported', message: ...}`, not a string;
 *   - `plan_code` is the literal string `'none'` when unsubscribed.
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import App from '../src/App'
import {
  EMPTY_OVERVIEW, installFetch, makeMe, OWNER_PERMISSIONS,
  RESTRICTED_PERMISSIONS, sessionRoutes,
} from './harness'

/** owner has billing:read + billing:write; admin has read only. */
const ADMIN_PERMISSIONS = OWNER_PERMISSIONS.filter((p) => p !== 'billing:write')

const STATUS = {
  plan_code: 'pro',
  plan_name: 'Pro',
  subscription_status: 'active',
  interval: 'month',
  provider: 'stripe',
  current_period_start: '2026-08-14T18:30:00.381377+00:00',
  current_period_end: '2026-09-14T18:30:00.381377+00:00',
  trial_end: null,
  cancel_at_period_end: false,
  pending_plan_code: null,
  last_invoice_status: 'paid',
  estimated_overage_cents: 1250,
  currency: 'usd',
  usage: {
    voice_minute: {
      included: 2000.0, used: 25.0, overage: 0, unit: 'minute',
      percent_used: 1.2, overage_cents: 0,
    },
    sms_segment: {
      included: 2500, used: 120, overage: 0, unit: 'segment',
      percent_used: 4.8, overage_cents: 0,
    },
  },
}

const PLANS = [
  {
    code: 'trial',
    name: 'Trial',
    description: '14 days to try the product. No card required.',
    currency: 'usd',
    monthly_price_cents: 0,
    annual_price_cents: null,
    included_voice_minutes: 60,
    included_sms_segments: 50,
    overage_voice_minute_cents: 0.0,
    overage_enabled: false,
    trial_days: 14,
    features: { team_members: 2, whatsapp: false, api_access: false },
    is_current: false,
  },
  {
    code: 'starter',
    name: 'Starter',
    description: 'One receptionist, one calendar, one CRM.',
    currency: 'usd',
    monthly_price_cents: 19900,
    annual_price_cents: 199000,
    included_voice_minutes: 500,
    included_sms_segments: 500,
    overage_voice_minute_cents: 12.0,
    overage_enabled: true,
    trial_days: 0,
    features: { team_members: 3, whatsapp: false, api_access: false },
    is_current: false,
  },
  {
    code: 'pro',
    name: 'Pro',
    description: 'Multi-location, outbound campaigns, full integrations.',
    currency: 'usd',
    monthly_price_cents: 49900,
    annual_price_cents: 499000,
    included_voice_minutes: 2000,
    included_sms_segments: 2500,
    overage_voice_minute_cents: 10.0,
    overage_enabled: true,
    trial_days: 0,
    features: { team_members: 10, whatsapp: true, api_access: true },
    is_current: true,
  },
  {
    code: 'enterprise',
    name: 'Enterprise',
    description: 'Unlimited seats and integrations. Invoiced.',
    currency: 'usd',
    monthly_price_cents: 149900,
    annual_price_cents: 1499000,
    included_voice_minutes: 10000,
    included_sms_segments: 10000,
    overage_voice_minute_cents: 8.0,
    overage_enabled: true,
    trial_days: 0,
    features: { team_members: -1, whatsapp: true, api_access: true },
    is_current: false,
  },
]

const USAGE = {
  billing_period: '2026-09',
  period_start: '2026-08-14T18:30:00.655558+00:00',
  period_end: '2026-09-14T18:30:00.655558+00:00',
  plan_code: 'pro',
  metrics: [
    {
      metric: 'voice_minute', unit: 'minute', included: 2000.0, used: 2125.0,
      remaining: 0.0, overage: 125.0, percent_used: 106.25, overage_cents: 1250,
    },
    {
      metric: 'sms_segment', unit: 'segment', included: 2500.0, used: 120.0,
      remaining: 2380.0, overage: 0.0, percent_used: 4.8, overage_cents: 0,
    },
    {
      metric: 'llm_token', unit: 'token', included: 0.0, used: 0.0,
      remaining: 0.0, overage: 0.0, percent_used: 0.0, overage_cents: 0,
    },
  ],
  estimated_overage_cents: 1250,
  currency: 'usd',
}

const INVOICES = [{
  id: 'in_test_123',
  status: 'paid',
  currency: 'usd',
  amount_due_cents: 29900,
  amount_paid_cents: 29900,
  period_start: '2026-07-14T18:30:00+00:00',
  period_end: '2026-08-14T18:30:00+00:00',
  hosted_invoice_url: 'https://invoice.stripe.com/i/acct_1/test_abc',
}]

function backend(me = makeMe(), extra = {}) {
  return {
    ...sessionRoutes(me),
    'GET /api/analytics/overview': { body: EMPTY_OVERVIEW },
    'GET /api/billing/plans': { body: PLANS },
    'GET /api/billing/usage': { body: USAGE },
    'GET /api/billing/invoices': { body: INVOICES },
    'GET /api/billing': { body: STATUS },
    ...extra,
  }
}

async function open(me = makeMe(), extra = {}) {
  window.location.hash = '#/billing'
  const fetched = installFetch(backend(me, extra))
  const view = render(<App />)
  await screen.findByRole('heading', { name: /^billing$/i, level: 1 })
  return { ...fetched, ...view }
}

const section = (name) => screen.getByRole('region', { name })

/** Wait for the four API-backed sections to have painted their data. */
async function ready() {
  await screen.findByText('Voice Minute')
}

let opened
beforeEach(() => {
  opened = vi.fn(() => null)
  vi.stubGlobal('open', opened)
})

/* ---------------------------------------------------------------- tests --- */

describe('route', () => {
  it('renders at /billing', async () => {
    await open()
    expect(await screen.findByRole('region', { name: /current subscription/i }))
      .toBeInTheDocument()
  })

  it('refuses the page without billing:read', async () => {
    window.location.hash = '#/billing'
    installFetch(backend(makeMe(RESTRICTED_PERMISSIONS)))
    render(<App />)
    expect(await screen.findByText(/you do not have access to this page/i))
      .toBeInTheDocument()
  })
})

describe('current subscription', () => {
  it('renders the plan, status and period from the API', async () => {
    await open()
    const card = await screen.findByRole('region', { name: /current subscription/i })

    expect(within(card).getByText('Active')).toBeInTheDocument()
    expect(within(card).getByText(/Pro/)).toBeInTheDocument()
    expect(within(card).getByText(/Monthly/)).toBeInTheDocument()
    expect(within(card).getByText(/Stripe/)).toBeInTheDocument()
  })

  it('shows the estimated overage as the server calculated it', async () => {
    await open()
    await ready()
    const card = section(/current subscription/i)
    // 1250 cents -> $12.50. Not recomputed from usage in the browser.
    expect(within(card).getByText(/\$12\.50/)).toBeInTheDocument()
  })

  it('warns when cancellation is scheduled, without saying cancelled', async () => {
    await open(makeMe(), {
      'GET /api/billing': {
        body: {
          ...STATUS, subscription_status: 'canceling', cancel_at_period_end: true,
        },
      },
    })
    await ready()
    const card = section(/current subscription/i)

    expect(within(card).getByText(/scheduled to cancel/i)).toBeInTheDocument()
    expect(within(card).getByText(/everything keeps working/i))
      .toBeInTheDocument()
    expect(within(card).getByText('Canceling')).toBeInTheDocument()
  })

  it('announces a scheduled plan change from pending_plan_code', async () => {
    await open(makeMe(), {
      'GET /api/billing': { body: { ...STATUS, pending_plan_code: 'starter' } },
    })
    expect(await screen.findByText(/a change to/i)).toBeInTheDocument()
    expect(screen.getByText(/stays active until then/i)).toBeInTheDocument()
  })

  it('shows a trial end date when the API sends one', async () => {
    await open(makeMe(), {
      'GET /api/billing': {
        body: {
          ...STATUS,
          subscription_status: 'trialing',
          trial_end: '2026-08-20T18:30:00+00:00',
        },
      },
    })
    await ready()
    const card = section(/current subscription/i)
    expect(within(card).getByText('Trialing')).toBeInTheDocument()
    expect(within(card).getByText(/20 Aug 2026/)).toBeInTheDocument()
  })
})

describe('plan catalogue', () => {
  it('renders every plan with real prices and entitlements', async () => {
    await open()
    await ready()
    const card = section(/available plans/i)

    expect(within(card).getByText('$499.00')).toBeInTheDocument()      // pro
    expect(within(card).getByText('$199.00')).toBeInTheDocument()      // starter
    expect(within(card).getByText(/\$1,990\.00 \/ year/)).toBeInTheDocument()
    expect(within(card).getByText(/2,000 included/)).toBeInTheDocument()
  })

  it('marks the current plan and offers no switch to it', async () => {
    await open()
    const pro = (await screen.findByRole('heading', { name: 'Pro', level: 4 }))
      .closest('.card')

    expect(within(pro).getByText('Current plan')).toBeInTheDocument()
    expect(within(pro).queryByRole('button', { name: /switch to/i })).toBeNull()
  })

  it('renders a free plan and a null annual price honestly', async () => {
    await open()
    const trial = (await screen.findByRole('heading', { name: 'Trial', level: 4 }))
      .closest('.card')

    expect(within(trial).getByText('$0.00')).toBeInTheDocument()
    // annual_price_cents is null -- no annual line invented.
    expect(within(trial).queryByText(/\/ year/)).toBeNull()
    expect(within(trial).getByText(/not available on this plan/i))
      .toBeInTheDocument()
    expect(within(trial).getAllByText(/14 days/).length).toBeGreaterThan(0)
  })

  it('renders -1 entitlements as Unlimited', async () => {
    const user = userEvent.setup()
    await open()
    const ent = (await screen.findByRole('heading', { name: 'Enterprise', level: 4 }))
      .closest('.card')

    await user.click(within(ent).getByText(/included features/i))
    expect(within(ent).getByText('Unlimited')).toBeInTheDocument()
  })
})

describe('usage', () => {
  it('renders per-metric usage from the usage endpoint', async () => {
    await open()
    await ready()
    const card = section(/usage this period/i)

    expect(within(card).getByText('Voice Minute')).toBeInTheDocument()
    expect(within(card).getByText('2,125')).toBeInTheDocument()
    expect(within(card).getByText(/2,000 included/)).toBeInTheDocument()
    expect(within(card).getByText(/2026-09/)).toBeInTheDocument()
  })

  it('uses the server percentage for the progress bar', async () => {
    await open()
    await ready()
    const card = section(/usage this period/i)
    const bars = within(card).getAllByRole('progressbar')

    // 106.25% is clamped to 100 for the bar width, but the API value drives it.
    expect(bars[0]).toHaveAttribute('aria-valuenow', '100')
    expect(bars[1]).toHaveAttribute('aria-valuenow', '5')
  })

  it('draws no progress bar for a metric with no allowance', async () => {
    // included: 0 makes a percentage meaningless.
    await open()
    await ready()
    const card = section(/usage this period/i)
    const tokens = within(card).getByText('Llm Token').closest('.card')

    expect(within(tokens).queryByRole('progressbar')).toBeNull()
    expect(within(tokens).getByText(/no included allowance/i))
      .toBeInTheDocument()
  })

  it('shows overage quantity and cost from the API', async () => {
    await open()
    await ready()
    const card = section(/usage this period/i)
    const voice = within(card).getByText('Voice Minute').closest('.card')

    expect(within(voice).getByText('125')).toBeInTheDocument()
    expect(within(voice).getByText(/\$12\.50/)).toBeInTheDocument()
  })

  it('labels the overage total as an estimate, not a bill', async () => {
    await open()
    await ready()
    const card = section(/usage this period/i)
    expect(within(card).getByText(/not a bill/i)).toBeInTheDocument()
  })
})

describe('invoices', () => {
  it('renders invoice rows with backend amounts and currency', async () => {
    await open()
    await ready()
    const card = section(/^invoices$/i)

    expect(within(card).getAllByText('$299.00')).toHaveLength(2)  // due + paid
    expect(within(card).getByText('paid')).toBeInTheDocument()
  })

  it('links to the hosted invoice with a severed opener', async () => {
    await open()
    await ready()
    const link = within(section(/^invoices$/i)).getByRole('link', { name: /view/i })

    expect(link).toHaveAttribute('href', INVOICES[0].hosted_invoice_url)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toMatch(/noopener/)
    expect(link.getAttribute('rel')).toMatch(/noreferrer/)
  })

  it('shows no link when the backend has no hosted URL', async () => {
    await open(makeMe(), {
      'GET /api/billing/invoices': {
        body: [{ ...INVOICES[0], hosted_invoice_url: null }],
      },
    })
    await ready()
    const card = section(/^invoices$/i)
    expect(within(card).queryByRole('link', { name: /view/i })).toBeNull()
  })
})

describe('permissions', () => {
  it('gives an admin read access but no mutation controls', async () => {
    // admin has billing:read; billing:write is owner-only.
    await open(makeMe(ADMIN_PERMISSIONS))
    await ready()

    expect(within(section(/current subscription/i)).getByText(/^Pro$/))
      .toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /cancel subscription/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /reconcile/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /manage payment details/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /switch to/i })).toBeNull()
  })

  it('gives an owner the full control set', async () => {
    await open(makeMe(OWNER_PERMISSIONS))
    await screen.findByRole('region', { name: /current subscription/i })

    expect(screen.getByRole('button', { name: /manage payment details/i }))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: /reconcile with provider/i }))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: /cancel subscription/i }))
      .toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /switch to/i }).length)
      .toBeGreaterThan(0)
  })

  it('offers no cancel button when cancellation is already scheduled', async () => {
    await open(makeMe(), {
      'GET /api/billing': { body: { ...STATUS, cancel_at_period_end: true } },
    })
    await screen.findByText(/scheduled to cancel/i)
    expect(screen.queryByRole('button', { name: /cancel subscription/i })).toBeNull()
  })
})

describe('portal', () => {
  it('opens the URL the backend returned, safely', async () => {
    const user = userEvent.setup()
    await open(makeMe(), {
      'POST /api/billing/portal': {
        body: { url: 'https://billing.stripe.com/p/session_abc' },
      },
    })

    await user.click(screen.getByRole('button', { name: /manage payment details/i }))

    await waitFor(() => expect(opened).toHaveBeenCalled())
    expect(opened).toHaveBeenCalledWith(
      'https://billing.stripe.com/p/session_abc',
      '_blank',
      'noopener,noreferrer'
    )
    expect(await screen.findByText(/portal opened in a new tab/i))
      .toBeInTheDocument()
  })

  it('explains an unsupported provider instead of showing an error', async () => {
    const user = userEvent.setup()
    await open(makeMe(), {
      // The real 501 shape: detail is an object, not a string.
      'POST /api/billing/portal': {
        status: 501,
        body: { detail: { code: 'unsupported', message: 'manual has no customer portal' } },
      },
    })

    await user.click(screen.getByRole('button', { name: /manage payment details/i }))
    expect(await screen.findByText(/manual has no customer portal/i))
      .toBeInTheDocument()
    expect(opened).not.toHaveBeenCalled()
  })

  it('refuses to open a non-https URL', async () => {
    const user = userEvent.setup()
    await open(makeMe(), {
      'POST /api/billing/portal': { body: { url: 'javascript:alert(1)' } },
    })

    await user.click(screen.getByRole('button', { name: /manage payment details/i }))
    expect(await screen.findByText(/did not return a usable portal link/i))
      .toBeInTheDocument()
    expect(opened).not.toHaveBeenCalled()
  })
})

describe('change plan', () => {
  it('shows both current and proposed plan before confirming', async () => {
    const user = userEvent.setup()
    const { calls } = await open()

    const starter = (await screen.findByRole('heading', { name: 'Starter', level: 4 }))
      .closest('.card')
    await user.click(within(starter).getByRole('button', { name: /switch to starter/i }))

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent('Pro')
    expect(dialog).toHaveTextContent('Starter')
    expect(dialog).toHaveTextContent(/\$199\.00/)
    // Nothing sent until confirmed.
    expect(calls.some((c) => c.path.includes('change-plan'))).toBe(false)
  })

  it('reports a downgrade as scheduled, not applied', async () => {
    const user = userEvent.setup()
    await open(makeMe(), {
      'POST /api/billing/change-plan': {
        // The backend returns the *unchanged* plan with a pending code.
        body: { ...STATUS, pending_plan_code: 'starter' },
      },
    })

    const starter = (await screen.findByRole('heading', { name: 'Starter', level: 4 }))
      .closest('.card')
    await user.click(within(starter).getByRole('button', { name: /switch to/i }))
    await user.click(
      within(await screen.findByRole('dialog'))
        .getByRole('button', { name: /confirm change/i })
    )

    expect(await screen.findByText(/scheduled: you move to starter/i))
      .toBeInTheDocument()
    // Must not claim the plan already changed.
    expect(screen.queryByText(/your plan is now Starter/i)).toBeNull()
  })

  it('reports an immediate upgrade from the returned status', async () => {
    const user = userEvent.setup()
    const { calls } = await open(makeMe(), {
      'POST /api/billing/change-plan': {
        body: { ...STATUS, plan_code: 'enterprise', plan_name: 'Enterprise' },
      },
    })

    const ent = (await screen.findByRole('heading', { name: 'Enterprise', level: 4 }))
      .closest('.card')
    await user.click(within(ent).getByRole('button', { name: /switch to/i }))
    await user.click(
      within(await screen.findByRole('dialog'))
        .getByRole('button', { name: /confirm change/i })
    )

    expect(await screen.findByText(/your plan is now Enterprise/i))
      .toBeInTheDocument()

    const body = JSON.parse(
      calls.find((c) => c.path.includes('change-plan')).options.body
    )
    // Plan code and interval only -- never a price or an amount.
    expect(body).toEqual({ plan_code: 'enterprise', interval: 'month' })
  })

  it('surfaces a provider failure without claiming success', async () => {
    const user = userEvent.setup()
    await open(makeMe(), {
      'POST /api/billing/change-plan': {
        status: 402,
        body: { detail: { code: 'card_declined', message: 'Your card was declined.' } },
      },
    })

    const starter = (await screen.findByRole('heading', { name: 'Starter', level: 4 }))
      .closest('.card')
    await user.click(within(starter).getByRole('button', { name: /switch to/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /confirm change/i }))

    expect(await within(dialog).findByText(/your card was declined/i))
      .toBeInTheDocument()
    expect(screen.queryByText(/your plan is now/i)).toBeNull()
  })
})

describe('checkout', () => {
  const UNSUBSCRIBED = {
    ...STATUS, plan_code: 'none', plan_name: 'No plan', last_invoice_status: null,
  }

  it('routes an unsubscribed tenant to checkout, not change-plan', async () => {
    const user = userEvent.setup()
    const { calls } = await open(makeMe(), {
      'GET /api/billing': { body: UNSUBSCRIBED },
      'GET /api/billing/plans': {
        body: PLANS.map((p) => ({ ...p, is_current: false })),
      },
      'POST /api/billing/checkout': {
        body: { url: 'https://checkout.stripe.com/c/pay/cs_test_abc' },
      },
    })

    const pro = (await screen.findByRole('heading', { name: 'Pro', level: 4 }))
      .closest('.card')
    await user.click(within(pro).getByRole('button', { name: /choose pro/i }))
    await user.click(
      within(await screen.findByRole('dialog'))
        .getByRole('button', { name: /continue to checkout/i })
    )

    await waitFor(() =>
      expect(calls.some((c) => c.path.includes('/billing/checkout'))).toBe(true))
    expect(calls.some((c) => c.path.includes('change-plan'))).toBe(false)
  })

  it('never claims payment succeeded, only that checkout opened', async () => {
    const user = userEvent.setup()
    await open(makeMe(), {
      'GET /api/billing': { body: UNSUBSCRIBED },
      'GET /api/billing/plans': {
        body: PLANS.map((p) => ({ ...p, is_current: false })),
      },
      'POST /api/billing/checkout': {
        body: { url: 'https://checkout.stripe.com/c/pay/cs_test_abc' },
      },
    })

    const pro = (await screen.findByRole('heading', { name: 'Pro', level: 4 }))
      .closest('.card')
    await user.click(within(pro).getByRole('button', { name: /choose pro/i }))
    await user.click(
      within(await screen.findByRole('dialog'))
        .getByRole('button', { name: /continue to checkout/i })
    )

    const flash = await screen.findByText(/checkout for Pro opened in a new tab/i)
    expect(flash).toHaveTextContent(/once the payment provider confirms it/i)
    expect(screen.queryByText(/payment (succeeded|complete)/i)).toBeNull()
  })
})

describe('cancellation', () => {
  it('confirms first and explains the period-end default', async () => {
    const user = userEvent.setup()
    const { calls } = await open()

    await user.click(screen.getByRole('button', { name: /cancel subscription/i }))
    const dialog = await screen.findByRole('dialog')

    expect(dialog).toHaveTextContent(/stays active until/i)
    expect(dialog).toHaveTextContent(/already paid for it/i)
    expect(calls.some((c) => c.path.includes('/billing/cancel'))).toBe(false)
  })

  it('says scheduled when the backend only scheduled it', async () => {
    const user = userEvent.setup()
    const { calls } = await open(makeMe(), {
      'POST /api/billing/cancel': {
        body: {
          ...STATUS, subscription_status: 'canceling', cancel_at_period_end: true,
        },
      },
    })

    await user.click(screen.getByRole('button', { name: /cancel subscription/i }))
    await user.click(
      within(await screen.findByRole('dialog'))
        .getByRole('button', { name: /confirm cancellation/i })
    )

    expect(await screen.findByText(/cancellation scheduled/i)).toBeInTheDocument()
    const body = JSON.parse(
      calls.find((c) => c.path.includes('/billing/cancel')).options.body
    )
    expect(body.immediately).toBe(false)
  })

  it('sends immediately only when the box is ticked', async () => {
    const user = userEvent.setup()
    const { calls } = await open(makeMe(), {
      'POST /api/billing/cancel': {
        body: {
          ...STATUS, subscription_status: 'canceled', cancel_at_period_end: false,
        },
      },
    })

    await user.click(screen.getByRole('button', { name: /cancel subscription/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('checkbox'))
    await user.click(within(dialog).getByRole('button', { name: /confirm cancellation/i }))

    expect(await screen.findByText(/subscription cancelled/i)).toBeInTheDocument()
    const body = JSON.parse(
      calls.find((c) => c.path.includes('/billing/cancel')).options.body
    )
    expect(body.immediately).toBe(true)
  })

  it('keeps the subscription when dismissed', async () => {
    const user = userEvent.setup()
    const { calls } = await open()

    await user.click(screen.getByRole('button', { name: /cancel subscription/i }))
    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /keep subscription/i }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(calls.some((c) => c.path.includes('/billing/cancel'))).toBe(false)
  })
})

describe('reconciliation', () => {
  it('calls the real endpoint and reports what it did', async () => {
    const user = userEvent.setup()
    const { calls } = await open(makeMe(), {
      'POST /api/billing/reconcile': { body: STATUS },
    })

    await user.click(screen.getByRole('button', { name: /reconcile with provider/i }))

    expect(await screen.findByText(/rebuilt this period's usage summary/i))
      .toBeInTheDocument()
    const call = calls.find((c) => c.path.includes('/billing/reconcile'))
    expect(call.method).toBe('POST')
    // No body: the tenant comes from the token.
    expect(call.options.body).toBeUndefined()
  })

  it('describes itself as reporting, not as fixing billing', async () => {
    await open()
    expect(screen.getByText(/does not change what you are charged/i))
      .toBeInTheDocument()
  })

  it('surfaces a reconciliation failure', async () => {
    const user = userEvent.setup()
    await open(makeMe(), {
      'POST /api/billing/reconcile': {
        status: 502,
        body: { detail: { code: 'temporary', message: 'stripe is unavailable' } },
      },
    })

    await user.click(screen.getByRole('button', { name: /reconcile with provider/i }))
    expect(await screen.findByText(/stripe is unavailable/i)).toBeInTheDocument()
  })
})

describe('states', () => {
  it('shows a loading state before data arrives', async () => {
    window.location.hash = '#/billing'
    let release
    const gate = new Promise((resolve) => { release = resolve })
    installFetch(backend(makeMe(), {
      'GET /api/billing/usage': async () => {
        await gate
        return { body: USAGE }
      },
    }))
    render(<App />)

    await screen.findByRole('heading', { name: /^billing$/i, level: 1 })
    expect(await screen.findByText(/loading your usage/i)).toBeInTheDocument()

    release()
    expect(await screen.findByText('Voice Minute')).toBeInTheDocument()
  })

  it('keeps other sections working when one endpoint fails', async () => {
    await open(makeMe(), {
      'GET /api/billing/invoices': { status: 500, body: { detail: 'boom' } },
    })

    expect(await screen.findByText(/could not load your invoices/i))
      .toBeInTheDocument()
    expect(screen.getByText('Voice Minute')).toBeInTheDocument()
  })

  it('shows empty states for a brand-new tenant', async () => {
    await open(makeMe(), {
      'GET /api/billing/invoices': { body: [] },
      'GET /api/billing/usage': { body: { ...USAGE, metrics: [] } },
    })

    expect(await screen.findByText(/no invoices yet/i)).toBeInTheDocument()
    expect(screen.getByText(/no usage recorded yet/i)).toBeInTheDocument()
  })

  it('shows an empty state when no plans are configured', async () => {
    await open(makeMe(), { 'GET /api/billing/plans': { body: [] } })
    expect(await screen.findByText(/no plans available/i)).toBeInTheDocument()
  })
})

describe('dates', () => {
  it('renders billing dates in the tenant timezone', async () => {
    await open()
    await ready()
    // 18:30 UTC is 14:30 in America/New_York, the fixture tenant's zone.
    const card = section(/current subscription/i)
    expect(within(card).getByText(/14 Aug 2026, 14:30/)).toBeInTheDocument()
    expect(within(card).queryByText(/18:30/)).toBeNull()
  })
})

describe('financial integrity', () => {
  it('shows no fabricated revenue metric anywhere', async () => {
    await open()
    await screen.findByText('Voice Minute')

    const text = document.body.textContent
    expect(text).not.toMatch(/estimated revenue/i)
    expect(text).not.toMatch(/revenue/i)
    expect(text).not.toMatch(/\bMRR\b|\bARR\b/)
  })

  it('keeps plan price, usage, overage and invoice total distinct', async () => {
    await open()
    await ready()
    // $499 plan, 2,125 minutes used, $12.50 overage, $299 invoice: four
    // different numbers from four different endpoints, never summed.
    expect(within(section(/available plans/i)).getByText('$499.00'))
      .toBeInTheDocument()
    expect(within(section(/usage this period/i)).getByText('2,125'))
      .toBeInTheDocument()
    expect(within(section(/usage this period/i)).getAllByText(/\$12\.50/).length)
      .toBeGreaterThan(0)
    expect(within(section(/^invoices$/i)).getAllByText('$299.00').length)
      .toBe(2)
  })

  it('preserves the backend currency rather than assuming dollars', async () => {
    await open(makeMe(), {
      'GET /api/billing/invoices': {
        body: [{ ...INVOICES[0], currency: 'eur' }],
      },
    })
    await ready()
    const card = section(/^invoices$/i)
    expect(within(card).getAllByText(/€299\.00/).length).toBe(2)
  })
})

describe('security', () => {
  it('never renders a secret the API should not have sent', async () => {
    await open(makeMe(), {
      'GET /api/billing': {
        body: {
          ...STATUS,
          // None of these are in BillingStatusOut.
          stripe_secret_key: 'sk_live_LEAKED',
          webhook_secret: 'whsec_LEAKED',
          external_customer_id: 'cus_LEAKED',
          external_subscription_id: 'sub_LEAKED',
        },
      },
    })
    await ready()

    const text = document.body.textContent
    for (const leak of ['sk_live_LEAKED', 'whsec_LEAKED', 'cus_LEAKED',
      'sub_LEAKED']) {
      expect(text).not.toContain(leak)
    }
  })

  it('puts nothing in storage', async () => {
    await open()
    await screen.findByText('Voice Minute')
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it('never sends a tenant id, customer id or price', async () => {
    const user = userEvent.setup()
    const { calls } = await open(makeMe(), {
      'POST /api/billing/change-plan': { body: STATUS },
    })

    const starter = (await screen.findByRole('heading', { name: 'Starter', level: 4 }))
      .closest('.card')
    await user.click(within(starter).getByRole('button', { name: /switch to/i }))
    await user.click(
      within(await screen.findByRole('dialog'))
        .getByRole('button', { name: /confirm change/i })
    )
    await waitFor(() =>
      expect(calls.some((c) => c.path.includes('change-plan'))).toBe(true))

    for (const call of calls) {
      expect(call.path).not.toContain('tenant_id')
      const body = call.options?.body
      if (typeof body === 'string') {
        for (const forbidden of ['tenant_id', 'customer_id', 'price',
          'amount', 'currency']) {
          expect(body).not.toContain(forbidden)
        }
      }
    }
  })

  it('renders hostile plan and error text as text, not markup', async () => {
    const hostile = '<img src=x onerror="window.__billPwned=1">'
    const { container } = await open(makeMe(), {
      'GET /api/billing': { body: { ...STATUS, plan_name: hostile } },
      'GET /api/billing/plans': {
        body: [{ ...PLANS[1], name: hostile, description: hostile }],
      },
    })
    await screen.findByRole('region', { name: /current subscription/i })

    expect(container.querySelector('img')).toBeNull()
    expect(window.__billPwned).toBeUndefined()
    expect(screen.getAllByText(/onerror/).length).toBeGreaterThan(0)
  })

  it('renders a hostile invoice URL without making it a link target', async () => {
    await open(makeMe(), {
      'GET /api/billing/invoices': {
        body: [{ ...INVOICES[0], hosted_invoice_url: 'javascript:alert(1)' }],
      },
    })
    await ready()
    const card = section(/^invoices$/i)
    const link = within(card).queryByRole('link', { name: /view/i })
    // Either no link, or one that is not a javascript: URL.
    expect(link?.getAttribute('href') ?? '').not.toMatch(/^javascript:/i)
  })
})