/**
 * Executive overview.
 *
 * Every figure comes from `/api/analytics/overview`, which aggregates
 * server-side. There is no computed metric in this file and no constant
 * multiplied by anything — the audit found `booked × $150` presented as
 * "Est. revenue captured", and requirement 33 forbids exactly that.
 *
 * Where the backend genuinely cannot produce a figure, the tile says so
 * instead of showing a zero. A zero reads as "you had none"; "not available"
 * reads as "we do not know", and those are different facts.
 */
import { useState } from 'react'

import DateRange from '../components/DateRange'
import { BarList } from '../components/charts'
import {
  AsyncSection, EmptyState, StatCard, UnavailableCard,
} from '../components/ui'
import { getOverview } from '../lib/api'
import {
  formatDuration, formatMinutes, formatMoney, formatNumber, formatPercent,
} from '../lib/format'
import { useApi } from '../lib/hooks'
import { PERMISSIONS as P } from '../lib/permissions'
import { DEFAULT_RANGE } from '../lib/ranges'
import { navigate } from '../lib/router'

export default function Overview({ me, can }) {
  const [range, setRange] = useState(DEFAULT_RANGE)
  const { data, error, loading, reload } = useApi(
    () => getOverview(range), [JSON.stringify(range)]
  )

  const timezone = data?.window?.timezone ?? me.tenant.timezone

  return (
    <>
      <div className="page__header">
        <div>
          <h1>Overview</h1>
          <p className="page__description">
            How the agent is performing for {me.tenant.name}.
          </p>
        </div>
        <DateRange value={range} onChange={setRange} timezone={timezone} />
      </div>

      <AsyncSection
        loading={loading} error={error} onRetry={reload} resource="the overview"
      >
        {data && <OverviewBody data={data} can={can} />}
      </AsyncSection>
    </>
  )
}

function OverviewBody({ data, can }) {
  const { calls, conversion, operations, integrations, usage } = data

  if (calls.total === 0) {
    return (
      <div className="card">
        <EmptyState
          icon="☎"
          title="No calls in this period"
          description="Once the agent answers its first call, the numbers here will fill in. Try a wider date range."
        />
      </div>
    )
  }

  return (
    <div className="stack">
      <section aria-label="Call volume">
        <div className="grid grid--stats">
          <StatCard
            label="Calls answered"
            value={formatNumber(calls.answered)}
            hint={`${formatNumber(calls.total)} received`}
          />
          <StatCard
            label="Missed"
            value={formatNumber(calls.missed)}
            hint={
              calls.missed
                ? 'Callers who did not reach the agent'
                : 'None missed'
            }
            tone={calls.missed ? 'warn' : undefined}
          />
          <StatCard
            label="Appointments booked"
            value={formatNumber(conversion.booked)}
            hint={`${formatPercent(operations.booking_rate)} of eligible calls`}
          />
          <StatCard
            label="Transferred to a human"
            value={formatNumber(calls.transferred)}
            hint={`${formatPercent(operations.transfer_rate)} of answered calls`}
          />
          <StatCard
            label="Voice minutes"
            value={formatMinutes(calls.minutes)}
            hint={`Average call ${formatDuration(operations.average_call_seconds)}`}
          />
        </div>
      </section>

      <div className="grid grid--halves">
        <section className="card" aria-label="Conversion">
          <div className="card__header"><h3>Conversion</h3></div>
          <div className="card__body">
            <BarList
              ariaLabel="Conversion counts for the selected period"
              items={[
                { label: 'Eligible calls', value: conversion.eligible_calls },
                { label: 'Booked', value: conversion.booked },
                { label: 'Leads created', value: conversion.leads_created },
                { label: 'Leads qualified', value: conversion.leads_qualified },
                { label: 'Appointments', value: conversion.appointments },
              ]}
            />
            <p className="stat__hint" style={{ marginTop: 12 }}>
              An <em>eligible</em> call is one the agent answered that lasted at
              least ten seconds. Wrong numbers and immediate hang-ups are
              excluded so the booking rate reflects the agent, not the phone
              book.
            </p>
            <button
              type="button" className="btn btn--link"
              onClick={() => navigate('/analytics')}
            >
              See the full funnel
            </button>
          </div>
        </section>

        <section className="card" aria-label="Operations">
          <div className="card__header"><h3>Operations</h3></div>
          <div className="card__body">
            <div className="kv">
              <div className="kv__key">Answer rate</div>
              <div className="kv__value">
                {formatPercent(operations.answer_rate)}
                <span className="muted"> — answered ÷ all calls</span>
              </div>
              <div className="kv__key">Booking rate</div>
              <div className="kv__value">
                {formatPercent(operations.booking_rate)}
                <span className="muted"> — booked ÷ eligible calls</span>
              </div>
              <div className="kv__key">Transfer rate</div>
              <div className="kv__value">
                {formatPercent(operations.transfer_rate)}
                <span className="muted"> — transferred ÷ answered</span>
              </div>
              <div className="kv__key">Failure rate</div>
              <div className="kv__value">
                {formatPercent(operations.failure_rate)}
                <span className="muted"> — failed ÷ all calls</span>
              </div>
              <div className="kv__key">Inbound / outbound</div>
              <div className="kv__value">
                {formatNumber(calls.inbound)} / {formatNumber(calls.outbound)}
              </div>
            </div>
          </div>
        </section>
      </div>

      <div className="grid grid--halves">
        <section className="card" aria-label="Integration health">
          <div className="card__header"><h3>Integration health</h3></div>
          <div className="card__body">
            <div className="kv">
              <div className="kv__key">CRM sync failures</div>
              <div className="kv__value">
                {integrations.crm_sync_failures === 0
                  ? <span className="badge badge--ok">None</span>
                  : (
                    <span className="badge badge--danger">
                      {formatNumber(integrations.crm_sync_failures)} failed
                    </span>
                  )}
              </div>
              <div className="kv__key">Calendar failures</div>
              <div className="kv__value">
                {integrations.calendar_failures === 0
                  ? <span className="badge badge--ok">None</span>
                  : (
                    <span className="badge badge--danger">
                      {formatNumber(integrations.calendar_failures)} failed
                    </span>
                  )}
              </div>
            </div>
            {can(P.INTEGRATION_READ) && (
              <button
                type="button" className="btn btn--link"
                style={{ marginTop: 10 }}
                onClick={() => navigate('/integrations')}
              >
                Manage integrations
              </button>
            )}
          </div>
        </section>

        <section className="card" aria-label="Plan usage">
          <div className="card__header"><h3>Plan usage</h3></div>
          <div className="card__body">
            {usage ? (
              <>
                <div className="kv">
                  <div className="kv__key">Plan</div>
                  <div className="kv__value">{usage.plan_code}</div>
                  <div className="kv__key">Billing period</div>
                  <div className="kv__value">{usage.billing_period}</div>
                  <div className="kv__key">Voice minutes</div>
                  <div className="kv__value">
                    {formatMinutes(usage.voice_minutes_used)} of{' '}
                    {formatMinutes(usage.voice_minutes_included)}
                    {' '}({formatPercent(usage.percent_used)})
                  </div>
                  <div className="kv__key">Estimated overage</div>
                  <div className="kv__value">
                    {formatMoney(usage.estimated_overage_cents, usage.currency)}
                  </div>
                </div>
                {can(P.BILLING_WRITE) && (
                  <button
                    type="button" className="btn btn--link"
                    style={{ marginTop: 10 }}
                    onClick={() => navigate('/billing')}
                  >
                    Manage plan
                  </button>
                )}
              </>
            ) : (
              <p className="muted" style={{ margin: 0 }}>
                Usage and plan figures are only visible to accounts with billing
                access.
              </p>
            )}
          </div>
        </section>
      </div>

      {/*
        The audit found a headline tile reading "Est. revenue captured:
        $<booked × 150>". It is deliberately replaced with an honest statement
        rather than removed silently, so anyone looking for the old number
        finds out where it went.
      */}
      <div className="grid grid--stats">
        <UnavailableCard
          label="Revenue captured"
          reason="VoxDesk does not know what an appointment is worth to you. Connect a CRM with deal values to report this."
        />
      </div>
    </div>
  )
}