/**
 * Leads.
 *
 * The editable surface is deliberately narrow: create and do-not-call. Those
 * are the two operations the backend actually supports
 * (`POST /tenants/{id}/leads` and `POST .../do-not-call`), and requirement 9
 * says not to invent editable fields the backend does not have — a form that
 * silently discards what you typed is worse than no form.
 */
import { useState } from 'react'

import {
  Alert, AsyncSection, DataTable, Dialog, EmptyState, Field, StatusBadge,
} from '../components/ui'
import { createLeads, listLeads, markLeadDnc } from '../lib/api'
import { formatNumber, formatPhone, humanise } from '../lib/format'
import { useAction, useApi } from '../lib/hooks'
import { PERMISSIONS as P } from '../lib/permissions'

export default function Leads({ me, can }) {
  const [status, setStatus] = useState('')
  const [creating, setCreating] = useState(false)
  const [flash, setFlash] = useState(null)

  const { data, error, loading, reload } = useApi(
    () => listLeads(me.tenant.id, { status: status || undefined, limit: 100 }),
    [status]
  )

  const dnc = useAction(
    (leadId) => markLeadDnc(me.tenant.id, leadId),
    { onSuccess: () => { setFlash('Marked as do-not-call.'); reload() } }
  )

  const columns = [
    {
      key: 'name', header: 'Name',
      // Caller-supplied. Rendered as a child; React escapes it.
      render: (lead) => lead.name || <span className="muted">Unknown</span>,
    },
    {
      key: 'phone', header: 'Phone',
      render: (lead) => <span className="mono">{formatPhone(lead.phone)}</span>,
    },
    {
      key: 'email', header: 'Email',
      render: (lead) => lead.email || <span className="muted">—</span>,
    },
    {
      key: 'company', header: 'Company',
      render: (lead) => lead.company || <span className="muted">—</span>,
    },
    {
      key: 'score', header: 'Score', numeric: true,
      render: (lead) => lead.score ?? <span className="muted">—</span>,
    },
    {
      key: 'attempts', header: 'Attempts', numeric: true,
      render: (lead) => formatNumber(lead.attempts),
    },
    {
      key: 'status', header: 'Status',
      render: (lead) => <StatusBadge status={lead.status} />,
    },
    {
      key: 'actions', header: 'Actions',
      render: (lead) => (
        can(P.LEAD_UPDATE) && lead.status !== 'do_not_call' ? (
          <button
            type="button" className="btn btn--small btn--danger"
            disabled={dnc.pending}
            onClick={() => dnc.run(lead.id)}
          >
            Do not call
          </button>
        ) : <span className="muted">—</span>
      ),
    },
  ]

  return (
    <>
      <div className="page__header">
        <div>
          <h1>Leads</h1>
          <p className="page__description">
            People the agent has spoken to, and lists queued for outbound
            campaigns.
          </p>
        </div>
        {can(P.LEAD_CREATE) && (
          <button
            type="button" className="btn btn--primary"
            onClick={() => setCreating(true)}
          >
            Add leads
          </button>
        )}
      </div>

      {flash && <Alert tone="ok" onDismiss={() => setFlash(null)}>{flash}</Alert>}
      {dnc.error && (
        <Alert tone="error" onDismiss={dnc.clearError}>{dnc.error.message}</Alert>
      )}

      <div className="toolbar">
        <label className="sr-only" htmlFor="lead-status">Status</label>
        <select
          id="lead-status" className="select" style={{ width: 'auto' }}
          value={status} onChange={(event) => setStatus(event.target.value)}
        >
          <option value="">Any status</option>
          <option value="new">New</option>
          <option value="queued">Queued</option>
          <option value="called">Called</option>
          <option value="qualified">Qualified</option>
          <option value="unqualified">Unqualified</option>
          <option value="failed">Failed</option>
          <option value="do_not_call">Do not call</option>
        </select>
      </div>

      <div className="card">
        <AsyncSection
          loading={loading} error={error} onRetry={reload} resource="leads"
          isEmpty={data && data.length === 0}
          empty={
            <EmptyState
              icon="◧"
              title="No leads yet"
              description="Leads appear when the agent takes a call, or when you import a list."
            />
          }
        >
          {data && (
            <DataTable
              caption="Leads" columns={columns} rows={data}
              keyOf={(lead) => lead.id}
            />
          )}
        </AsyncSection>
      </div>

      <CreateLeadDialog
        open={creating} tenantId={me.tenant.id}
        onClose={() => setCreating(false)}
        onCreated={(count) => {
          setCreating(false)
          setFlash(`Added ${count} lead${count === 1 ? '' : 's'}.`)
          reload()
        }}
      />
    </>
  )
}

function CreateLeadDialog({ open, tenantId, onClose, onCreated }) {
  const [name, setName] = useState('')
  const [phone, setPhone] = useState('')
  const [email, setEmail] = useState('')
  const [notes, setNotes] = useState('')

  const create = useAction(
    () => createLeads(tenantId, [{ name, phone, email: email || null, notes }]),
    { onSuccess: (result) => onCreated(result?.created ?? 1) }
  )

  return (
    <Dialog
      open={open} title="Add a lead" onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button
            type="button" className="btn btn--primary"
            disabled={create.pending || !phone.trim()}
            onClick={() => create.run()}
          >
            {create.pending ? 'Adding…' : 'Add lead'}
          </button>
        </>
      }
    >
      {create.error && <Alert tone="error">{create.error.message}</Alert>}

      <Field label="Phone number" htmlFor="lead-phone"
             hint="Required. Duplicates within this account are skipped.">
        <input
          id="lead-phone" className="input" value={phone}
          onChange={(event) => setPhone(event.target.value)}
          placeholder="+1 555 010 2000"
        />
      </Field>
      <Field label="Name" htmlFor="lead-name">
        <input
          id="lead-name" className="input" value={name}
          onChange={(event) => setName(event.target.value)}
        />
      </Field>
      <Field label="Email" htmlFor="lead-email">
        <input
          id="lead-email" type="email" className="input" value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
      </Field>
      <Field label="Notes" htmlFor="lead-notes">
        <textarea
          id="lead-notes" className="textarea" value={notes}
          onChange={(event) => setNotes(event.target.value)}
        />
      </Field>
    </Dialog>
  )
}
