/**
 * Analytics.
 *
 * Every chart is fed by a backend aggregate. Nothing on this page divides,
 * multiplies or sums — requirement 12 wants defined formulas, and the only
 * way to keep a formula defined is to have exactly one implementation of it.
 * The definitions are printed on the page next to the numbers, because a rate
 * whose denominator nobody can name is a number nobody can defend in a
 * quarterly review.
 */
import { useState } from 'react'

import DateRange from '../components/DateRange'
import { BarList, DailyColumns, Funnel, UsageMeter } from '../components/charts'
import { AsyncSection, EmptyState, StatCard } from '../components/ui'
import { getCallAnalytics, getConversion, getUsageAnalytics } from '../lib/api'
import {
  formatDuration, formatMinutes, formatMoney, formatNumber, formatPercent,
} from '../lib/format'
import { useApi } from '../lib/hooks'
import { PERMISSIONS as P } from '../lib/permissions'
import { DEFAULT_RANGE } from '../lib/ranges'

export default function Analytics({ me, can }) {
  const [range, setRange] = useState(DEFAULT_RANGE)
  const key = JSON.stringify(range)

  const calls = useApi(() => getCallAnalytics(range), [key])
  const conversion = useApi(() => getConversion(range), [key])
  const usage = useApi(
    () => getUsageAnalytics(), [], { skip: !can(P.BILLING_READ) }
  )

  const timezone = calls.data?.window?.timezone ?? me.tenant.timezone

  return (
    <>
      <div className="page__header">
        <div>
          <h1>Analytics</h1>
          <p className="page__description">
            Call volume, conversion and metered usage. Every figure is computed
            server-side from your own records.
          </p>
        </div>
        <DateRange value={range} onChange={setRange} timezone={timezone} />
      </div>

      <div className="stack">
        <section className="card" aria-label="Call volume">
          <div className="card__header"><h3>Call volume</h3></div>
          <AsyncSection
            loading={calls.loading} error={calls.error} onRetry={calls.reload}
            resource="call analytics"
            isEmpty={calls.data && calls.data.totals.total === 0}
            empty={
              <EmptyState
                icon="◭" title="No calls in this period"
                description="Pick a wider range, or wait for the agent's first call."
              />
            }
          >
            {calls.data && <CallSection data={calls.data} />}
          </AsyncSection>
        </section>

        <section className="card" aria-label="Conversion funnel">
          <div className="card__header"><h3>Conversion</h3></div>
          <AsyncSection
            loading={conversion.loading} error={conversion.error}
            onRetry={conversion.reload} resource="conversion"
          >
            {conversion.data && <ConversionSection data={conversion.data} />}
          </AsyncSection>
        </section>

        {can(P.BILLING_READ) && (
          <section className="card" aria-label="Metered usage">
            <div className="card__header">
              <h3>Metered usage</h3>
              <span className="muted" style={{ fontSize: 12 }}>
                Current billing period — not the range above
              </span>
            </div>
            <AsyncSection
              loading={usage.loading} error={usage.error} onRetry={usage.reload}
              resource="usage"
            >
              {usage.data && <UsageSection data={usage.data} />}
            </AsyncSection>
          </section>
        )}
      </div>
    </>
  )
}

function CallSection({ data }) {
  const { totals, by_status: byStatus, by_direction: byDirection, series } = data
  const busiest = [...data.by_hour].sort((a, b) => b.total - a.total).slice(0, 6)

  return (
    <div className="card__body stack">
      <div className="grid grid--stats">
        <StatCard label="Total calls" value={formatNumber(totals.total)} />
        <StatCard label="Answered" value={formatNumber(totals.answered)} />
        <StatCard
          label="Missed" value={formatNumber(totals.missed)}
          tone={totals.missed ? 'warn' : undefined}
        />
        <StatCard
          label="Failed" value={formatNumber(totals.failed)}
          tone={totals.failed ? 'danger' : undefined}
        />
        <StatCard
          label="Average length"
          value={formatDuration(totals.average_seconds)}
          hint="Across answered calls only"
        />
      </div>

      <div>
        <h4 style={{ marginBottom: 8 }}>Daily volume</h4>
        <DailyColumns series={series} ariaLabel="Calls per day by outcome" />
      </div>

      <div className="grid grid--halves">
        <div>
          <h4 style={{ marginBottom: 8 }}>By outcome</h4>
          <BarList
            ariaLabel="Calls by outcome"
            items={[
              { label: 'Answered', value: byStatus.answered },
              { label: 'Missed', value: byStatus.missed, color: '#fbbf24' },
              { label: 'Failed', value: byStatus.failed, color: '#f87171' },
              { label: 'Other', value: byStatus.other, color: '#9ca3af' },
            ]}
          />
        </div>
        <div>
          <h4 style={{ marginBottom: 8 }}>Busiest hours</h4>
          {busiest.every((hour) => hour.total === 0) ? (
            <p className="muted">Not enough calls to show a pattern yet.</p>
          ) : (
            <BarList
              ariaLabel="Busiest hours of the day, in your local time"
              items={busiest.map((hour) => ({
                label: `${String(hour.hour).padStart(2, '0')}:00`,
                value: hour.total,
              }))}
            />
          )}
          <p className="stat__hint">
            Local hours in your business timezone. Inbound and outbound
            together: {formatNumber(byDirection.inbound)} in,{' '}
            {formatNumber(byDirection.outbound)} out.
          </p>
        </div>
      </div>
    </div>
  )
}

function ConversionSection({ data }) {
  const { funnel, rates } = data

  return (
    <div className="card__body stack">
      <Funnel stages={funnel} />

      <div className="grid grid--stats">
        <StatCard
          label="Answer rate" value={formatPercent(rates.answer_rate)}
          hint="answered ÷ all calls"
        />
        <StatCard
          label="Booking rate" value={formatPercent(rates.booking_rate)}
          hint="booked ÷ eligible calls"
        />
        <StatCard
          label="Transfer rate" value={formatPercent(rates.transfer_rate)}
          hint="transferred ÷ answered"
        />
        <StatCard
          label="Appointments kept" value={formatPercent(rates.appointment_kept_rate)}
          hint="attended ÷ booked"
        />
        <StatCard
          label="Failure rate" value={formatPercent(rates.failure_rate)}
          hint="failed ÷ all calls"
        />
      </div>

      <p className="stat__hint">
        An <strong>eligible</strong> call is one the agent answered that lasted
        at least ten seconds — the same threshold the outbound dialer uses to
        decide a lead was reached. Wrong numbers and immediate hang-ups are
        excluded from the booking denominator so the rate measures the agent
        rather than the phone book.
      </p>
    </div>
  )
}

function UsageSection({ data }) {
  return (
    <div className="card__body">
      <div className="row" style={{ justifyContent: 'space-between', marginBottom: 14 }}>
        <span>
          Plan <strong>{data.plan_code}</strong> — period {data.billing_period}
        </span>
        <span>
          Estimated overage{' '}
          <strong>{formatMoney(data.estimated_overage_cents, data.currency)}</strong>
        </span>
      </div>

      {data.metrics.map((metric) => (
        <UsageMeter
          key={metric.metric}
          label={metric.metric.replace(/_/g, ' ')}
          used={metric.used}
          included={metric.included}
          unit={`${metric.unit}s`}
          percent={metric.percent_used}
          overage={metric.overage}
        />
      ))}

      <p className="stat__hint">
        Usage belongs to the billing period anchored on your subscription, not
        to the date range above. Slicing it by an arbitrary range would produce
        a number that looks like a bill and is not one.
      </p>
    </div>
  )
}
