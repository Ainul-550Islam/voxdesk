/**
 * Outbound campaigns.
 *
 * Requirement 11: a button click must never bypass STEP 7's entitlement
 * enforcement. It does not, and cannot — `run_campaign_tick` calls
 * `billing.hooks.may_place_outbound_call` before every dial, so a tenant out
 * of minutes on a plan with overage disabled is refused at the point the money
 * would be spent, whatever the browser does.
 *
 * The UI's job is to make that legible in advance rather than to enforce it:
 * showing the plan position next to the Run button means an operator finds out
 * before they click, not after.
 */
import { useState } from 'react'

import { Alert, AsyncSection, DataTable, EmptyState, StatusBadge } from '../components/ui'
import { getUsageAnalytics, listCampaigns, runCampaign } from '../lib/api'
import { formatNumber, formatPercent } from '../lib/format'
import { useAction, useApi } from '../lib/hooks'
import { PERMISSIONS as P } from '../lib/permissions'

export default function Campaigns({ me, can }) {
  const [flash, setFlash] = useState(null)

  const { data, error, loading, reload } = useApi(
    () => listCampaigns(me.tenant.id), []
  )
  const usage = useApi(
    () => getUsageAnalytics(), [], { skip: !can(P.BILLING_READ) }
  )

  const run = useAction(
    (campaignId) => runCampaign(me.tenant.id, campaignId),
    {
      onSuccess: (result) => {
        setFlash(
          result?.dialed
            ? `Dialling ${result.dialed} lead${result.dialed === 1 ? '' : 's'}.`
            : 'No leads were ready to dial.'
        )
        reload()
      },
    }
  )

  const voice = usage.data?.metrics?.find((m) => m.metric === 'voice_minute')

  const columns = [
    { key: 'name', header: 'Campaign', render: (c) => c.name },
    {
      key: 'goal', header: 'Goal',
      render: (c) => <span className="muted">{c.goal}</span>,
    },
    {
      key: 'active', header: 'Status',
      render: (c) => (
        <StatusBadge status={c.is_active ? 'active' : 'paused'}
                     label={c.is_active ? 'Active' : 'Paused'} />
      ),
    },
    {
      key: 'leads', header: 'Leads', numeric: true,
      render: (c) => formatNumber(c.lead_count ?? 0),
    },
    {
      key: 'called', header: 'Called', numeric: true,
      render: (c) => formatNumber(c.called_count ?? 0),
    },
    {
      key: 'actions', header: 'Actions',
      render: (c) => can(P.CAMPAIGN_RUN) ? (
        <button
          type="button" className="btn btn--small"
          disabled={run.pending || !c.is_active}
          onClick={() => run.run(c.id)}
          title={c.is_active ? undefined : 'Activate the campaign to run it'}
        >
          Run a batch
        </button>
      ) : <span className="muted">—</span>,
    },
  ]

  return (
    <>
      <div className="page__header">
        <div>
          <h1>Campaigns</h1>
          <p className="page__description">
            Outbound calling batches. Every dial is checked against your plan
            before it is placed.
          </p>
        </div>
      </div>

      {flash && <Alert tone="ok" onDismiss={() => setFlash(null)}>{flash}</Alert>}
      {run.error && (
        <Alert tone="error" onDismiss={run.clearError}>{run.error.message}</Alert>
      )}

      {voice && (
        <Alert tone={voice.overage > 0 ? 'warn' : 'info'}>
          You have used {formatNumber(voice.used)} of{' '}
          {formatNumber(voice.included)} included voice minutes this period
          ({formatPercent(voice.percent_used)}).
          {voice.overage > 0 && (
            <> Further calls are billed as overage, or refused if your plan does
            not allow it.</>
          )}
        </Alert>
      )}

      <div className="card">
        <AsyncSection
          loading={loading} error={error} onRetry={reload} resource="campaigns"
          isEmpty={data && data.length === 0}
          empty={
            <EmptyState
              icon="➤" title="No campaigns"
              description="Campaigns are created through the API. Once one exists it will appear here."
            />
          }
        >
          {data && (
            <DataTable
              caption="Campaigns" columns={columns} rows={data}
              keyOf={(campaign) => campaign.id}
            />
          )}
        </AsyncSection>
      </div>
    </>
  )
}
