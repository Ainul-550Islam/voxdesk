/**
 * The call log.
 *
 * Backend pagination and backend filtering (requirements 6 and 23). The audit
 * found the old page fetching whatever the default returned and filtering
 * nothing, so there was no way to reach call 51 and no way to ask "show me
 * yesterday's missed calls".
 *
 * Search is debounced and sent to the server. Filtering in the browser would
 * only filter the page you already have, which looks like it works until a
 * tenant has more than one page.
 */
import { useEffect, useState } from 'react'

import { AsyncSection, DataTable, EmptyState, Pager, StatusBadge } from '../components/ui'
import { listCalls } from '../lib/api'
import { formatDateTime, formatDuration, formatPhone, humanise } from '../lib/format'
import { useApi, useDebounced, usePagination } from '../lib/hooks'
import { navigate } from '../lib/router'

const STATUSES = [
  ['', 'Any status'],
  ['completed', 'Answered'],
  ['no_answer', 'Missed'],
  ['failed', 'Failed'],
  ['in_progress', 'In progress'],
  ['ringing', 'Ringing'],
  ['transferred', 'Transferred'],
]

export default function Calls({ me }) {
  const [filters, setFilters] = useState({
    status: '', direction: '', booked: '', transferred: '', search: '',
  })
  const search = useDebounced(filters.search)
  const pager = usePagination(25)
  const timezone = me.tenant.timezone

  // Any filter change resets to page 1. Staying on page 7 of a result set
  // that now has two pages shows an empty table and looks broken.
  useEffect(() => {
    pager.reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters.status, filters.direction, filters.booked, filters.transferred, search])

  const params = {
    limit: pager.limit,
    offset: pager.offset,
    status: filters.status || undefined,
    direction: filters.direction || undefined,
    booked: filters.booked === '' ? undefined : filters.booked === 'yes',
    transferred: filters.transferred === '' ? undefined : filters.transferred === 'yes',
    search: search || undefined,
  }

  const { data, error, loading, reload } = useApi(
    () => listCalls(me.tenant.id, params),
    [JSON.stringify(params)]
  )

  const set = (key) => (event) =>
    setFilters((previous) => ({ ...previous, [key]: event.target.value }))

  const columns = [
    {
      key: 'when', header: 'When',
      render: (call) => (
        <button
          type="button" className="table__row-button"
          onClick={() => navigate(`/calls/${call.id}`)}
        >
          {formatDateTime(call.started_at, timezone)}
        </button>
      ),
    },
    {
      key: 'direction', header: 'Direction',
      render: (call) => <span className="muted">{humanise(call.direction)}</span>,
    },
    {
      key: 'caller', header: 'Caller',
      render: (call) => (
        <span className="mono">
          {formatPhone(call.direction === 'inbound' ? call.from : call.to)}
        </span>
      ),
    },
    {
      key: 'duration', header: 'Duration', numeric: true,
      render: (call) => formatDuration(call.duration),
    },
    {
      key: 'status', header: 'Status',
      render: (call) => <StatusBadge status={call.status} />,
    },
    {
      key: 'outcome', header: 'Outcome',
      render: (call) => (
        <div className="row" style={{ gap: 4 }}>
          {call.booked && <span className="badge badge--ok">Booked</span>}
          {call.escalated && <span className="badge badge--warn">Transferred</span>}
          {!call.booked && !call.escalated && (
            <span className="muted">{call.intent ? humanise(call.intent) : '—'}</span>
          )}
        </div>
      ),
    },
    {
      key: 'recording', header: 'Recording',
      render: (call) => call.has_recording
        ? <span className="badge badge--info">Available</span>
        : <span className="muted">—</span>,
    },
  ]

  const total = data?.total ?? 0

  return (
    <>
      <div className="page__header">
        <div>
          <h1>Calls</h1>
          <p className="page__description">
            Every call the agent handled. Select a row for the transcript and
            full detail.
          </p>
        </div>
      </div>

      <div className="toolbar">
        <div className="toolbar__grow">
          <label className="sr-only" htmlFor="call-search">Search by phone number</label>
          <input
            id="call-search" type="search" className="input"
            placeholder="Search phone number…"
            value={filters.search} onChange={set('search')}
          />
        </div>

        <label className="sr-only" htmlFor="call-status">Status</label>
        <select
          id="call-status" className="select" style={{ width: 'auto' }}
          value={filters.status} onChange={set('status')}
        >
          {STATUSES.map(([value, label]) => (
            <option key={value} value={value}>{label}</option>
          ))}
        </select>

        <label className="sr-only" htmlFor="call-direction">Direction</label>
        <select
          id="call-direction" className="select" style={{ width: 'auto' }}
          value={filters.direction} onChange={set('direction')}
        >
          <option value="">Any direction</option>
          <option value="inbound">Inbound</option>
          <option value="outbound">Outbound</option>
        </select>

        <label className="sr-only" htmlFor="call-booked">Booked</label>
        <select
          id="call-booked" className="select" style={{ width: 'auto' }}
          value={filters.booked} onChange={set('booked')}
        >
          <option value="">Booked or not</option>
          <option value="yes">Booked</option>
          <option value="no">Not booked</option>
        </select>

        <label className="sr-only" htmlFor="call-transferred">Transferred</label>
        <select
          id="call-transferred" className="select" style={{ width: 'auto' }}
          value={filters.transferred} onChange={set('transferred')}
        >
          <option value="">Transferred or not</option>
          <option value="yes">Transferred</option>
          <option value="no">Not transferred</option>
        </select>
      </div>

      <div className="card">
        <AsyncSection
          loading={loading} error={error} onRetry={reload} resource="calls"
          isEmpty={data && total === 0}
          empty={
            <EmptyState
              icon="☎"
              title="No calls match these filters"
              description="Try widening the search, or clearing a filter."
            />
          }
        >
          {data && (
            <>
              <DataTable
                caption="Calls"
                columns={columns}
                rows={data.calls}
                keyOf={(call) => call.id}
              />
              <Pager
                page={pager.page} pageCount={pager.pageCount(total)}
                total={total} limit={pager.limit}
                onPage={pager.setPage} label="calls"
              />
            </>
          )}
        </AsyncSection>
      </div>
    </>
  )
}