/**
 * Billing.
 *
 * Backed entirely by `app/api/billing_routes.py`:
 *
 *   GET  /api/billing            subscription status + a usage summary
 *   GET  /api/billing/plans      the catalogue, with `is_current` per plan
 *   GET  /api/billing/usage      per-metric metering for the period
 *   GET  /api/billing/invoices   invoice metadata mirrored locally
 *   POST /api/billing/checkout   -> {url}
 *   POST /api/billing/portal     -> {url}
 *   POST /api/billing/change-plan
 *   POST /api/billing/cancel
 *   POST /api/billing/reconcile
 *
 * ## Money
 *
 * Every figure on this page comes from the API. Cents are formatted for
 * display and never recomputed: the server already returns
 * `overage_cents`, `estimated_overage_cents` and `percent_used`, so the
 * browser doing its own arithmetic would eventually disagree with the
 * invoice. `formatMoney` divides by 100 and hands the rest to `Intl`, which
 * is presentation, not calculation.
 *
 * The four quantities are kept visually distinct and are never added
 * together: plan price, usage, overage, invoice total. There is no
 * "estimated revenue" anywhere -- that was the fake metric removed in
 * phase 1, and a test asserts it has not come back.
 *
 * ## Honesty about provider state
 *
 * A checkout or portal call returns a URL and nothing else. Reaching a URL
 * proves nothing about payment, so this page opens the URL and says a
 * purchase was *started* -- never that it succeeded. Only a verified webhook
 * changes the subscription, and the page reflects that by re-reading the
 * status rather than assuming.
 *
 * Likewise cancellation: the backend distinguishes `canceling`
 * (`cancel_at_period_end: true`, service continues) from `canceled`. The UI
 * reports whichever the response actually contains.
 */
import { useCallback, useMemo, useState } from 'react'

import {
  Alert, AsyncSection, DataTable, Dialog, EmptyState, StatCard, StatusBadge,
} from '../components/ui'
import {
  cancelSubscription, changePlan, getBilling, getInvoices, getPlans, getUsage,
  openPortal, reconcileBilling, startCheckout,
} from '../lib/api'
import {
  formatDateTime, formatMoney, formatNumber, humanise, safeExternalUrl,
} from '../lib/format'
import { useAction, useApi } from '../lib/hooks'
import { PERMISSIONS as P } from '../lib/permissions'

/**
 * A provider that cannot do hosted checkout or a portal answers 501 with
 * `{code: 'unsupported'}`. That is configuration, not a fault, so it is
 * reported as an explanation rather than an error.
 */
const isUnsupported = (error) =>
  error?.status === 501 || error?.detail?.code === 'unsupported'

/**
 * Is this a URL we are willing to navigate to?
 *
 * `hosted_invoice_url` is stored from a provider payload, so it is not ours.
 * Delegates to the shared allowlist in `lib/format` -- the same one guarding
 * calendar `meeting_url` -- so there is exactly one URL policy to review.
 */
const safeHref = (url) => safeExternalUrl(url)

/** Open a provider URL the backend just handed us, defensively. */
function openExternal(url) {
  // `javascript:` in a window.open is a real XSS sink, and this value
  // originates outside our system.
  const safe = safeExternalUrl(url)
  if (!safe) return false
  // `noopener` severs `window.opener`, so the payment page cannot navigate
  // this tab. `noreferrer` keeps the dashboard URL out of their logs.
  window.open(safe, '_blank', 'noopener,noreferrer')
  return true
}

export default function Billing({ me, can }) {
  const timezone = me.tenant.timezone
  const mayWrite = can(P.BILLING_WRITE)

  const [flash, setFlash] = useState(null)
  const [confirming, setConfirming] = useState(null)

  const billing = useApi(() => getBilling(), [])
  const plans = useApi(() => getPlans(), [])
  const usage = useApi(() => getUsage(), [])
  const invoices = useApi(() => getInvoices(), [])

  const refresh = useCallback(() => {
    billing.reload()
    usage.reload()
    plans.reload()
    invoices.reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [billing.reload, usage.reload, plans.reload, invoices.reload])

  const status = billing.data

  return (
    <>
      <div className="page__header">
        <div>
          <h1>Billing</h1>
          <p className="page__description">
            Your plan, what you have used this period, and your invoices.
          </p>
        </div>
        {mayWrite && <PortalButton onFlash={setFlash} />}
      </div>

      {flash && (
        <Alert tone={flash.tone} onDismiss={() => setFlash(null)}>
          {flash.message}
        </Alert>
      )}

      <Subscription
        billing={billing}
        timezone={timezone}
        mayWrite={mayWrite}
        onCancel={() => setConfirming({ kind: 'cancel' })}
        onReconciled={(message) => { setFlash(message); refresh() }}
      />

      <Usage usage={usage} timezone={timezone} />

      <Plans
        plans={plans}
        status={status}
        mayWrite={mayWrite}
        onChoose={(plan) => setConfirming({ kind: 'plan', plan })}
      />

      <Invoices invoices={invoices} timezone={timezone} />

      <ConfirmDialog
        confirming={confirming}
        status={status}
        timezone={timezone}
        onClose={() => setConfirming(null)}
        onDone={(message) => {
          setConfirming(null)
          setFlash(message)
          refresh()
        }}
      />
    </>
  )
}

/* --------------------------------------------------------- subscription --- */

/** Tones for the real `SubscriptionStatus` enum. */
const STATUS_TONE = {
  active: 'ok', trialing: 'info', past_due: 'warn', canceling: 'warn',
  canceled: 'danger', incomplete: 'warn', incomplete_expired: 'danger',
  paused: 'muted',
}

function Subscription({ billing, timezone, mayWrite, onCancel, onReconciled }) {
  const status = billing.data

  const reconcile = useAction(() => reconcileBilling(), {
    onSuccess: () => onReconciled({
      tone: 'ok',
      message: 'Re-read the provider and rebuilt this period\'s usage summary.',
    }),
  })

  return (
    <section className="card" aria-label="Current subscription">
      <div className="card__header">
        <h3>Current plan</h3>
        {status && (
          <span className={`badge badge--${STATUS_TONE[status.subscription_status] ?? 'muted'}`}>
            {humanise(status.subscription_status)}
          </span>
        )}
      </div>
      <div className="card__body">
        <AsyncSection
          loading={billing.loading}
          error={billing.error}
          onRetry={billing.reload}
          resource="your subscription"
        >
          {status && (
            <>
              {/* The backend reports `canceling` separately from `canceled`.
                  Saying "cancelled" while service continues would be wrong. */}
              {status.cancel_at_period_end && (
                <Alert tone="warn">
                  This subscription is scheduled to cancel
                  {status.current_period_end
                    ? ` on ${formatDateTime(status.current_period_end, timezone)}`
                    : ' at the end of the current period'}
                  . Until then everything keeps working.
                </Alert>
              )}

              {status.pending_plan_code && (
                <Alert tone="info">
                  A change to <strong>{status.pending_plan_code}</strong> is
                  scheduled for the end of this period. Your current plan
                  stays active until then.
                </Alert>
              )}

              <dl className="kv">
                <dt>Plan</dt>
                <dd>{status.plan_name} <span className="muted">({status.plan_code})</span></dd>

                <dt>Billing interval</dt>
                <dd>{humanise(status.interval)}ly</dd>

                <dt>Current period</dt>
                <dd>
                  {status.current_period_start && status.current_period_end
                    ? `${formatDateTime(status.current_period_start, timezone)} – ${formatDateTime(status.current_period_end, timezone)}`
                    : '—'}
                </dd>

                {status.trial_end && (
                  <>
                    <dt>Trial ends</dt>
                    <dd>{formatDateTime(status.trial_end, timezone)}</dd>
                  </>
                )}

                <dt>Estimated overage</dt>
                {/* The server's figure, not a browser calculation. It is an
                    estimate for the period so far, not an invoice. */}
                <dd>
                  {formatMoney(status.estimated_overage_cents, status.currency)}
                  <span className="muted"> so far this period</span>
                </dd>

                {status.last_invoice_status && (
                  <>
                    <dt>Last invoice</dt>
                    <dd><StatusBadge status={status.last_invoice_status} /></dd>
                  </>
                )}

                {/* Which provider is configured -- never a key or secret. */}
                <dt>Payment provider</dt>
                <dd>{humanise(status.provider)}</dd>
              </dl>

              {mayWrite && (
                <div className="toolbar">
                  <button
                    type="button" className="btn btn--small"
                    disabled={reconcile.pending}
                    onClick={() => reconcile.run()}
                  >
                    {reconcile.pending ? 'Reconciling…' : 'Reconcile with provider'}
                  </button>
                  {!status.cancel_at_period_end
                    && status.subscription_status !== 'canceled' && (
                    <button
                      type="button" className="btn btn--small btn--danger"
                      onClick={onCancel}
                    >
                      Cancel subscription
                    </button>
                  )}
                </div>
              )}

              {mayWrite && (
                <p className="muted" style={{ fontSize: 12, margin: '8px 0 0' }}>
                  Reconciling re-reads your subscription from the payment
                  provider and rebuilds this period&apos;s usage summary. It
                  does not change what you are charged.
                </p>
              )}

              {reconcile.error && (
                <Alert tone="error" onDismiss={reconcile.clearError}>
                  {reconcile.error.message}
                </Alert>
              )}
            </>
          )}
        </AsyncSection>
      </div>
    </section>
  )
}

function PortalButton({ onFlash }) {
  const portal = useAction(() => openPortal(), {
    onSuccess: (result) => {
      // Only claim it opened if a usable URL actually came back.
      onFlash(openExternal(result?.url)
        ? { tone: 'ok', message: 'The billing portal opened in a new tab.' }
        : {
          tone: 'error',
          message: 'The provider did not return a usable portal link.',
        })
    },
  })

  if (portal.error && isUnsupported(portal.error)) {
    return (
      <span className="muted" style={{ fontSize: 13 }}>
        {portal.error.message}
      </span>
    )
  }

  return (
    <div className="stack" style={{ gap: 4 }}>
      <button
        type="button" className="btn"
        disabled={portal.pending} onClick={() => portal.run()}
      >
        {portal.pending ? 'Opening…' : 'Manage payment details'}
      </button>
      {portal.error && !isUnsupported(portal.error) && (
        <span className="field__error" role="alert">{portal.error.message}</span>
      )}
    </div>
  )
}

/* ---------------------------------------------------------------- usage --- */

function Usage({ usage, timezone }) {
  const data = usage.data
  const metrics = data?.metrics ?? []

  return (
    <section className="card" aria-label="Usage this period">
      <div className="card__header">
        <div>
          <h3>Usage this period</h3>
          {data && (
            <p className="muted" style={{ margin: '2px 0 0', fontSize: 13 }}>
              {data.billing_period} ·{' '}
              {formatDateTime(data.period_start, timezone)} –{' '}
              {formatDateTime(data.period_end, timezone)}
            </p>
          )}
        </div>
      </div>
      <div className="card__body">
        <AsyncSection
          loading={usage.loading}
          error={usage.error}
          onRetry={usage.reload}
          resource="your usage"
          isEmpty={Boolean(data) && metrics.length === 0}
          empty={
            <EmptyState
              icon="$"
              title="No usage recorded yet"
              description="Usage appears here once the agent starts handling calls and messages."
            />
          }
        >
          {metrics.length > 0 && (
            <>
              <div className="grid grid--stats">
                {metrics.map((metric) => (
                  <MetricCard
                    key={metric.metric}
                    metric={metric}
                    currency={data.currency}
                  />
                ))}
              </div>
              <p style={{ marginBottom: 0 }}>
                <strong>Estimated overage this period: </strong>
                {formatMoney(data.estimated_overage_cents, data.currency)}
                <span className="muted">
                  {' '}— an estimate from usage so far, not a bill.
                </span>
              </p>
            </>
          )}
        </AsyncSection>
      </div>
    </section>
  )
}

function MetricCard({ metric, currency }) {
  // `percent_used` comes from the server. An unlimited or unmetered plan
  // reports included=0, where a percentage is meaningless -- so the bar is
  // only drawn when there is a real allowance to be a fraction of.
  const hasAllowance = metric.included > 0
  const percent = Math.min(100, Math.max(0, metric.percent_used))
  const over = metric.overage > 0

  return (
    <div className="card">
      <div className="card__body">
        <div className="stat__label">{humanise(metric.metric)}</div>
        <div className="stat__value" style={{ fontSize: 22 }}>
          {formatNumber(metric.used)}
          <span className="muted" style={{ fontSize: 14 }}>
            {' '}{metric.unit}{metric.used === 1 ? '' : 's'}
          </span>
        </div>

        {hasAllowance ? (
          <>
            <div
              className="meter"
              role="progressbar"
              aria-valuenow={Math.round(percent)}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-label={`${humanise(metric.metric)} allowance used`}
            >
              <div
                className={`meter__fill${over ? ' meter__fill--over' : ''}`}
                style={{ width: `${percent}%` }}
              />
            </div>
            <div className="stat__hint">
              {formatNumber(metric.included)} included ·{' '}
              {formatNumber(metric.remaining)} remaining
            </div>
          </>
        ) : (
          <div className="stat__hint">No included allowance on this plan.</div>
        )}

        {over && (
          <div className="stat__hint">
            <strong>{formatNumber(metric.overage)}</strong> over ·{' '}
            {formatMoney(metric.overage_cents, currency)}
          </div>
        )}
      </div>
    </div>
  )
}

/* ---------------------------------------------------------------- plans --- */

/** Feature entitlements use -1 for unlimited. */
const featureValue = (value) => {
  if (value === -1) return 'Unlimited'
  if (value === true) return 'Yes'
  if (value === false) return 'No'
  return formatNumber(value)
}

function Plans({ plans, status, mayWrite, onChoose }) {
  const list = plans.data ?? []
  // The subscription reports `plan_code: 'none'` when nothing is subscribed.
  const subscribed = Boolean(status) && status.plan_code !== 'none'

  return (
    <section className="card" aria-label="Available plans">
      <div className="card__header"><h3>Plans</h3></div>
      <div className="card__body">
        <AsyncSection
          loading={plans.loading}
          error={plans.error}
          onRetry={plans.reload}
          resource="the plan catalogue"
          isEmpty={Boolean(plans.data) && list.length === 0}
          empty={
            <EmptyState
              icon="$"
              title="No plans available"
              description="No active plans are configured on this deployment."
            />
          }
        >
          {list.length > 0 && (
            <div className="grid grid--halves">
              {list.map((plan) => (
                <PlanCard
                  key={plan.code}
                  plan={plan}
                  subscribed={subscribed}
                  pending={status?.pending_plan_code === plan.code}
                  mayWrite={mayWrite}
                  onChoose={() => onChoose(plan)}
                />
              ))}
            </div>
          )}
        </AsyncSection>
      </div>
    </section>
  )
}

function PlanCard({ plan, subscribed, pending, mayWrite, onChoose }) {
  return (
    <div className="card">
      <div className="card__header">
        <div>
          <h4 style={{ margin: 0 }}>{plan.name}</h4>
          <div className="muted" style={{ fontSize: 12 }}>{plan.code}</div>
        </div>
        {plan.is_current && <span className="badge badge--ok">Current plan</span>}
        {pending && !plan.is_current && (
          <span className="badge badge--info">Scheduled</span>
        )}
      </div>
      <div className="card__body">
        <div className="stat__value" style={{ fontSize: 24 }}>
          {formatMoney(plan.monthly_price_cents, plan.currency)}
          <span className="muted" style={{ fontSize: 14 }}> / month</span>
        </div>
        {plan.annual_price_cents != null && (
          <div className="stat__hint">
            or {formatMoney(plan.annual_price_cents, plan.currency)} / year
          </div>
        )}

        <p style={{ fontSize: 13.5 }}>{plan.description}</p>

        <dl className="kv">
          <dt>Voice minutes</dt>
          <dd>{formatNumber(plan.included_voice_minutes)} included</dd>
          <dt>SMS segments</dt>
          <dd>{formatNumber(plan.included_sms_segments)} included</dd>
          <dt>Overage</dt>
          <dd>
            {plan.overage_enabled
              ? `${plan.overage_voice_minute_cents}¢ per extra minute`
              : 'Not available on this plan'}
          </dd>
          {plan.trial_days > 0 && (
            <>
              <dt>Trial</dt>
              <dd>{plan.trial_days} days</dd>
            </>
          )}
        </dl>

        {Object.keys(plan.features ?? {}).length > 0 && (
          <details>
            <summary style={{ cursor: 'pointer', fontSize: 13 }}>
              Included features
            </summary>
            <dl className="kv" style={{ marginTop: 8 }}>
              {Object.entries(plan.features).map(([key, value]) => (
                <FeatureRow key={key} label={humanise(key)} value={value} />
              ))}
            </dl>
          </details>
        )}

        {mayWrite && !plan.is_current && (
          <div className="toolbar">
            <button type="button" className="btn btn--primary" onClick={onChoose}>
              {subscribed ? `Switch to ${plan.name}` : `Choose ${plan.name}`}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

function FeatureRow({ label, value }) {
  return (
    <>
      <dt>{label}</dt>
      <dd>{featureValue(value)}</dd>
    </>
  )
}

/* ------------------------------------------------------------- invoices --- */

function Invoices({ invoices, timezone }) {
  const rows = invoices.data ?? []

  const columns = useMemo(() => [
    {
      key: 'period', header: 'Period',
      render: (row) => (row.period_start
        ? `${formatDateTime(row.period_start, timezone, { hour: undefined, minute: undefined })} – ${formatDateTime(row.period_end, timezone, { hour: undefined, minute: undefined })}`
        : '—'),
    },
    {
      key: 'status', header: 'Status',
      render: (row) => <StatusBadge status={row.status} />,
    },
    {
      key: 'due', header: 'Amount due', numeric: true,
      render: (row) => formatMoney(row.amount_due_cents, row.currency),
    },
    {
      key: 'paid', header: 'Amount paid', numeric: true,
      render: (row) => formatMoney(row.amount_paid_cents, row.currency),
    },
    {
      key: 'link', header: 'Invoice',
      render: (row) => (safeHref(row.hosted_invoice_url) ? (
        <a
          href={safeHref(row.hosted_invoice_url)}
          // An external, provider-controlled URL: sever the opener and the
          // referrer.
          target="_blank"
          rel="noopener noreferrer external"
        >
          View
        </a>
      ) : <span className="muted">—</span>),
    },
  ], [timezone])

  return (
    <section className="card" aria-label="Invoices">
      <div className="card__header"><h3>Invoices</h3></div>
      <div className="card__body">
        <AsyncSection
          loading={invoices.loading}
          error={invoices.error}
          onRetry={invoices.reload}
          resource="your invoices"
          isEmpty={Boolean(invoices.data) && rows.length === 0}
          empty={
            <EmptyState
              icon="$"
              title="No invoices yet"
              description="Invoices appear here after your first billing period closes."
            />
          }
        >
          {rows.length > 0 && (
            <DataTable
              caption="Invoices"
              columns={columns}
              rows={rows}
              keyOf={(row) => row.id}
            />
          )}
        </AsyncSection>
      </div>
    </section>
  )
}

/* --------------------------------------------------------------- dialog --- */

/**
 * Confirmation for the two consequential mutations.
 *
 * Plan changes route to `checkout` when there is no subscription yet and to
 * `change-plan` when there is -- which is what the backend expects, and the
 * difference matters: one returns a URL to visit, the other returns the new
 * billing status.
 */
function ConfirmDialog({ confirming, status, timezone, onClose, onDone }) {
  const [immediately, setImmediately] = useState(false)
  const kind = confirming?.kind
  const plan = confirming?.plan
  const subscribed = Boolean(status) && status.plan_code !== 'none'

  const change = useAction(
    () => (subscribed
      ? changePlan(plan.code, status.interval)
      : startCheckout(plan.code, status?.interval ?? 'month')),
    {
      onSuccess: (result) => {
        if (!subscribed) {
          // Checkout returns {url}. Reaching a payment page is not a payment.
          onDone(openExternal(result?.url)
            ? {
              tone: 'info',
              message: `Checkout for ${plan.name} opened in a new tab. Your plan changes once the payment provider confirms it.`,
            }
            : {
              tone: 'error',
              message: 'The provider did not return a usable checkout link.',
            })
          return
        }
        // change-plan returns the new BillingStatusOut. An upgrade applies
        // now; a downgrade is scheduled. Report whichever happened.
        onDone(result?.pending_plan_code
          ? {
            tone: 'info',
            message: `Scheduled: you move to ${result.pending_plan_code} at the end of this period.`,
          }
          : {
            tone: 'ok',
            message: `Your plan is now ${result?.plan_name ?? plan.name}.`,
          })
      },
    }
  )

  const cancel = useAction(
    () => cancelSubscription(immediately, ''),
    {
      onSuccess: (result) => {
        // Never say "cancelled" when the backend only scheduled it.
        onDone(result?.cancel_at_period_end
          ? {
            tone: 'ok',
            message: 'Cancellation scheduled. Your subscription stays active until the end of the current period.',
          }
          : {
            tone: 'ok',
            message: `Subscription cancelled. Status is now ${humanise(result?.subscription_status ?? 'canceled')}.`,
          })
        setImmediately(false)
      },
    }
  )

  const action = kind === 'cancel' ? cancel : change
  const title = kind === 'cancel'
    ? 'Cancel this subscription?'
    : subscribed ? `Switch to ${plan?.name}?` : `Subscribe to ${plan?.name}?`

  return (
    <Dialog
      open={Boolean(confirming)}
      title={title}
      onClose={onClose}
      footer={
        <>
          <button
            type="button" className="btn" onClick={onClose}
            disabled={action.pending}
          >
            {kind === 'cancel' ? 'Keep subscription' : 'Cancel'}
          </button>
          <button
            type="button"
            className={kind === 'cancel' ? 'btn btn--danger' : 'btn btn--primary'}
            onClick={() => action.run()}
            disabled={action.pending}
          >
            {action.pending
              ? 'Working…'
              : kind === 'cancel'
                ? 'Confirm cancellation'
                : subscribed ? 'Confirm change' : 'Continue to checkout'}
          </button>
        </>
      }
    >
      {kind === 'cancel' ? (
        <div className="stack">
          <p style={{ margin: 0 }}>
            By default your subscription stays active until{' '}
            {status?.current_period_end
              ? formatDateTime(status.current_period_end, timezone)
              : 'the end of the current period'}
            , because you have already paid for it.
          </p>
          <label className="row" style={{ gap: 8, alignItems: 'flex-start' }}>
            <input
              type="checkbox"
              checked={immediately}
              disabled={cancel.pending}
              onChange={(event) => setImmediately(event.target.checked)}
            />
            <span>
              Cancel immediately instead, ending service now without a refund
              for the remainder of the period.
            </span>
          </label>
        </div>
      ) : plan ? (
        <div className="stack">
          {/* Both plans stated explicitly, so the change is never ambiguous. */}
          <dl className="kv">
            <dt>Current plan</dt>
            <dd>
              {/* The status endpoint reports the plan name, not its price,
                  so no price is shown here rather than a guessed one. */}
              {subscribed ? status.plan_name : 'No plan'}
              {subscribed && (
                <span className="muted"> ({status.plan_code})</span>
              )}
            </dd>
            <dt>New plan</dt>
            <dd>
              {plan.name} — {formatMoney(plan.monthly_price_cents, plan.currency)}
              /month
            </dd>
          </dl>
          <p style={{ margin: 0 }}>
            {subscribed
              ? 'An upgrade takes effect immediately and is prorated by the provider. A downgrade is scheduled for the end of the current period.'
              : 'You will be taken to the payment provider to complete this purchase. Your plan changes only once they confirm the payment.'}
          </p>
        </div>
      ) : null}

      {action.error && (
        <Alert tone="error" onDismiss={action.clearError}>
          {action.error.message}
        </Alert>
      )}
    </Dialog>
  )
}
