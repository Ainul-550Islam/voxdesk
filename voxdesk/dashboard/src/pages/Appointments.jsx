/**
 * Appointments.
 *
 * All scheduling logic stays in the STEP 6 service. This page sends a plan —
 * a cancel, or a new local wall-clock time — and renders whatever the backend
 * decides. Requirement 10 is explicit: no calendar business logic in the
 * frontend, and the reason is that the browser cannot see the provider's
 * calendar, the business hours or the buffers.
 *
 * Times render in **the appointment's own timezone**, which STEP 6 stores per
 * row rather than deriving from the tenant — a business that relocates must
 * not silently reinterpret appointments already in the book.
 */
import { useState } from 'react'

import {
  Alert, AsyncSection, DataTable, Dialog, EmptyState, Field, StatusBadge,
} from '../components/ui'
import { cancelAppointment, listAppointments, rescheduleAppointment } from '../lib/api'
import { formatDateTime, formatPhone, safeExternalUrl } from '../lib/format'
import { useAction, useApi } from '../lib/hooks'
import { PERMISSIONS as P } from '../lib/permissions'

const VIEWS = [
  { value: 'upcoming', label: 'Upcoming' },
  { value: 'all', label: 'All' },
  { value: 'cancelled', label: 'Cancelled' },
  { value: 'no_show', label: 'No-shows' },
]

export default function Appointments({ me, can }) {
  const [view, setView] = useState('upcoming')
  const [flash, setFlash] = useState(null)
  const [rescheduling, setRescheduling] = useState(null)

  const params =
    view === 'upcoming' ? { upcoming: true, limit: 100 }
      : view === 'all' ? { limit: 100 }
        : { status: view, limit: 100 }

  const { data, error, loading, reload } = useApi(
    () => listAppointments(params), [view]
  )

  const cancel = useAction(
    (id) => cancelAppointment(id, 'Cancelled from the dashboard'),
    { onSuccess: () => { setFlash('Appointment cancelled.'); reload() } }
  )

  const columns = [
    {
      key: 'when', header: 'When',
      render: (appointment) => (
        <>
          {formatDateTime(appointment.starts_at, appointment.timezone)}
          <div className="muted" style={{ fontSize: 12 }}>
            {appointment.timezone}
          </div>
        </>
      ),
    },
    {
      key: 'customer', header: 'Customer',
      render: (appointment) => (
        <>
          {appointment.customer_name || <span className="muted">Unknown</span>}
          <div className="mono muted" style={{ fontSize: 12 }}>
            {formatPhone(appointment.customer_phone)}
          </div>
        </>
      ),
    },
    {
      key: 'reason', header: 'Type',
      render: (appointment) => appointment.reason || <span className="muted">—</span>,
    },
    {
      key: 'status', header: 'Status',
      render: (appointment) => <StatusBadge status={appointment.status} />,
    },
    {
      key: 'provider', header: 'Calendar',
      render: (appointment) => (
        <>
          <span className="muted">{appointment.provider ?? '—'}</span>
          {appointment.external_event_id && (
            <div className="badge badge--ok" style={{ marginTop: 2 }}>Confirmed</div>
          )}
        </>
      ),
    },
    {
      key: 'meeting', header: 'Meeting',
      // `meeting_url` is provider-supplied, so it is allowlisted before it
      // becomes an href. A rejected value falls through to the same "—" a
      // missing URL shows.
      render: (appointment) => {
        const href = safeExternalUrl(appointment.meeting_url)
        return href ? (
          <a href={href} target="_blank" rel="noopener noreferrer">
            Join
          </a>
        ) : <span className="muted">—</span>
      },
    },
    {
      key: 'actions', header: 'Actions',
      render: (appointment) => {
        const active = ['pending', 'confirmed', 'rescheduled'].includes(
          appointment.status
        )
        if (!can(P.APPOINTMENT_WRITE) || !active) {
          return <span className="muted">—</span>
        }
        return (
          <div className="row" style={{ gap: 4 }}>
            <button
              type="button" className="btn btn--small"
              onClick={() => setRescheduling(appointment)}
            >
              Reschedule
            </button>
            <button
              type="button" className="btn btn--small btn--danger"
              disabled={cancel.pending}
              onClick={() => cancel.run(appointment.id)}
            >
              Cancel
            </button>
          </div>
        )
      },
    },
  ]

  const rows = data?.appointments ?? []

  return (
    <>
      <div className="page__header">
        <div>
          <h1>Appointments</h1>
          <p className="page__description">
            Bookings the agent made, and their state on the connected calendar.
          </p>
        </div>
      </div>

      {flash && <Alert tone="ok" onDismiss={() => setFlash(null)}>{flash}</Alert>}
      {cancel.error && (
        <Alert tone="error" onDismiss={cancel.clearError}>
          {cancel.error.message}
        </Alert>
      )}

      <div className="toolbar" role="tablist" aria-label="Appointment view">
        {VIEWS.map((option) => (
          <button
            key={option.value} type="button" role="tab"
            aria-selected={view === option.value}
            className={`btn btn--small${view === option.value ? ' btn--primary' : ''}`}
            onClick={() => setView(option.value)}
          >
            {option.label}
          </button>
        ))}
      </div>

      <div className="card">
        <AsyncSection
          loading={loading} error={error} onRetry={reload} resource="appointments"
          isEmpty={data && rows.length === 0}
          empty={
            <EmptyState
              icon="▤"
              title="No appointments here"
              description="Bookings the agent makes will appear on this page."
            />
          }
        >
          <DataTable
            caption="Appointments" columns={columns} rows={rows}
            keyOf={(appointment) => appointment.id}
          />
        </AsyncSection>
      </div>

      <RescheduleDialog
        appointment={rescheduling}
        onClose={() => setRescheduling(null)}
        onDone={() => {
          setRescheduling(null)
          setFlash('Appointment moved.')
          reload()
        }}
      />
    </>
  )
}

function RescheduleDialog({ appointment, onClose, onDone }) {
  const [when, setWhen] = useState('')

  const move = useAction(
    () => rescheduleAppointment(appointment.id, when, 'Moved from the dashboard'),
    { onSuccess: onDone }
  )

  return (
    <Dialog
      open={Boolean(appointment)} title="Reschedule appointment" onClose={onClose}
      footer={
        <>
          <button type="button" className="btn" onClick={onClose}>Cancel</button>
          <button
            type="button" className="btn btn--primary"
            disabled={move.pending || !when}
            onClick={() => move.run()}
          >
            {move.pending ? 'Moving…' : 'Move appointment'}
          </button>
        </>
      }
    >
      {move.error && <Alert tone="error">{move.error.message}</Alert>}

      <Field
        label="New time" htmlFor="reschedule-when"
        hint={
          appointment
            ? `Local time in ${appointment.timezone}. The server checks business hours, buffers and the connected calendar before accepting.`
            : ''
        }
      >
        <input
          id="reschedule-when" type="datetime-local" className="input"
          value={when} onChange={(event) => setWhen(event.target.value)}
        />
      </Field>
    </Dialog>
  )
}