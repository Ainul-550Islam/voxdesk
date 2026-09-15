/**
 * One call, in full.
 *
 * Assembled from two requests: `/api/calls/{id}` (metadata, transfer,
 * appointment, lead, CRM sync state) and `/api/calls/{id}/transcript`. The
 * detail endpoint joins server-side so the page is not a request per panel —
 * requirement 32.
 *
 * The recording URL is returned by the backend **only** when the account has
 * `recording:read`. The page does not decide that; it renders what it was
 * given. A transcript is operational data, but the audio is the customer's
 * voice.
 */
import { useCallback } from 'react'

import Transcript from '../components/Transcript'
import { AsyncSection, StatusBadge } from '../components/ui'
import { getCall, getTranscript } from '../lib/api'
import {
  formatDateTime, formatDuration, formatPhone, humanise, safeExternalUrl,
} from '../lib/format'
import { useApi } from '../lib/hooks'
import { PERMISSIONS as P } from '../lib/permissions'
import { navigate } from '../lib/router'

export default function CallDetail({ me, can, callId }) {
  const timezone = me.tenant.timezone

  const call = useApi(() => getCall(callId), [callId])
  const transcript = useApi(
    () => getTranscript(callId),
    [callId],
    { skip: !can(P.TRANSCRIPT_READ) }
  )

  const copy = useCallback(() => {
    const text = (transcript.data ?? [])
      .map((turn) => `${turn.speaker}: ${turn.text}`)
      .join('\n')
    navigator.clipboard?.writeText(text)
  }, [transcript.data])

  return (
    <>
      <div className="page__header">
        <div>
          <button
            type="button" className="btn btn--link"
            onClick={() => navigate('/calls')}
          >
            ← Back to calls
          </button>
          <h1 style={{ marginTop: 6 }}>Call detail</h1>
        </div>
      </div>

      <AsyncSection
        loading={call.loading} error={call.error} onRetry={call.reload}
        resource="this call"
      >
        {call.data && (
          <div className="stack">
            <Summary call={call.data} timezone={timezone} can={can} />

            <div className="grid grid--halves">
              <TransferPanel call={call.data} timezone={timezone} />
              <OutcomePanel call={call.data} timezone={timezone} />
            </div>

            <CrmPanel call={call.data} timezone={timezone} />

            <section className="card" aria-label="Transcript">
              <div className="card__header"><h3>Transcript</h3></div>
              <div className="card__body">
                {can(P.TRANSCRIPT_READ) ? (
                  <AsyncSection
                    loading={transcript.loading} error={transcript.error}
                    onRetry={transcript.reload} resource="the transcript"
                  >
                    <Transcript
                      turns={transcript.data ?? []}
                      agentName={me.tenant.agent_name}
                      timezone={timezone}
                      onCopy={copy}
                    />
                  </AsyncSection>
                ) : (
                  <p className="muted" style={{ margin: 0 }}>
                    Your role does not include access to transcripts.
                  </p>
                )}
              </div>
            </section>
          </div>
        )}
      </AsyncSection>
    </>
  )
}

function Summary({ call, timezone, can }) {
  return (
    <section className="card" aria-label="Call summary">
      <div className="card__header">
        <h3>{formatPhone(call.direction === 'inbound' ? call.from : call.to)}</h3>
        <div className="row">
          <StatusBadge status={call.status} />
          {call.booked && <span className="badge badge--ok">Booked</span>}
          {call.escalated && <span className="badge badge--warn">Transferred</span>}
        </div>
      </div>
      <div className="card__body">
        <div className="kv">
          <div className="kv__key">Started</div>
          <div className="kv__value">{formatDateTime(call.started_at, timezone)}</div>
          <div className="kv__key">Ended</div>
          <div className="kv__value">
            {call.ended_at ? formatDateTime(call.ended_at, timezone) : '—'}
          </div>
          <div className="kv__key">Duration</div>
          <div className="kv__value">{formatDuration(call.duration)}</div>
          <div className="kv__key">Direction</div>
          <div className="kv__value">{humanise(call.direction)}</div>
          <div className="kv__key">From → to</div>
          <div className="kv__value mono">
            {formatPhone(call.from)} → {formatPhone(call.to)}
          </div>
          <div className="kv__key">Intent</div>
          <div className="kv__value">{call.intent ? humanise(call.intent) : '—'}</div>
          <div className="kv__key">Lead score</div>
          <div className="kv__value">{call.lead_score ?? '—'}</div>
          <div className="kv__key">Model</div>
          <div className="kv__value">{call.llm_used || '—'}</div>
          <div className="kv__key">Call SID</div>
          <div className="kv__value mono">{call.call_sid}</div>
        </div>

        {call.summary && (
          <div style={{ marginTop: 14 }}>
            <div className="kv__key" style={{ marginBottom: 4 }}>Summary</div>
            {/* Model-generated text. Rendered as a child, never as HTML. */}
            <div className="turn__text">{call.summary}</div>
          </div>
        )}

        {call.has_recording && (
          <div style={{ marginTop: 14 }}>
            <div className="kv__key" style={{ marginBottom: 4 }}>Recording</div>
            {can(P.RECORDING_READ) && call.recording_url ? (
              // eslint-disable-next-line jsx-a11y/media-has-caption
              <audio controls src={call.recording_url} style={{ width: '100%' }}>
                Your browser cannot play this recording.
              </audio>
            ) : (
              <p className="muted" style={{ margin: 0 }}>
                A recording exists, but your role does not include permission to
                play it.
              </p>
            )}
          </div>
        )}
      </div>
    </section>
  )
}

function TransferPanel({ call, timezone }) {
  const transfer = call.transfer ?? {}
  return (
    <section className="card" aria-label="Transfer">
      <div className="card__header"><h3>Human transfer</h3></div>
      <div className="card__body">
        {transfer.state === 'none' ? (
          <p className="muted" style={{ margin: 0 }}>
            This call was not transferred.
          </p>
        ) : (
          <div className="kv">
            <div className="kv__key">State</div>
            <div className="kv__value"><StatusBadge status={transfer.state} /></div>
            <div className="kv__key">Reason</div>
            <div className="kv__value">{transfer.reason || '—'}</div>
            <div className="kv__key">Destination</div>
            <div className="kv__value mono">
              {transfer.destination ? formatPhone(transfer.destination) : '—'}
            </div>
            <div className="kv__key">Requested</div>
            <div className="kv__value">
              {transfer.started_at
                ? formatDateTime(transfer.started_at, timezone) : '—'}
            </div>
            <div className="kv__key">Connected</div>
            <div className="kv__value">
              {transfer.completed_at
                ? formatDateTime(transfer.completed_at, timezone) : '—'}
            </div>
            {transfer.failed_at && (
              <>
                <div className="kv__key">Failed</div>
                <div className="kv__value">
                  {formatDateTime(transfer.failed_at, timezone)}
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </section>
  )
}

function OutcomePanel({ call, timezone }) {
  return (
    <section className="card" aria-label="Outcome">
      <div className="card__header"><h3>Appointment &amp; lead</h3></div>
      <div className="card__body">
        {call.appointment ? (
          <div className="kv">
            <div className="kv__key">Appointment</div>
            <div className="kv__value">
              <StatusBadge status={call.appointment.status} />
            </div>
            <div className="kv__key">Starts</div>
            <div className="kv__value">
              {formatDateTime(
                call.appointment.starts_at, call.appointment.timezone || timezone
              )}
              <span className="muted"> ({call.appointment.timezone})</span>
            </div>
            <div className="kv__key">Customer</div>
            <div className="kv__value">{call.appointment.customer_name}</div>
            {/* Provider-supplied: allowlisted before it becomes an href.
                An unsafe or malformed URL renders no Meeting row at all. */}
            {safeExternalUrl(call.appointment.meeting_url) && (
              <>
                <div className="kv__key">Meeting</div>
                <div className="kv__value">
                  <a
                    href={safeExternalUrl(call.appointment.meeting_url)}
                    target="_blank" rel="noopener noreferrer"
                  >
                    Join link
                  </a>
                </div>
              </>
            )}
          </div>
        ) : (
          <p className="muted" style={{ margin: 0 }}>
            No appointment came from this call.
          </p>
        )}

        {call.lead && (
          <div className="kv" style={{ marginTop: 14 }}>
            <div className="kv__key">Lead</div>
            <div className="kv__value">{call.lead.name || '—'}</div>
            <div className="kv__key">Status</div>
            <div className="kv__value"><StatusBadge status={call.lead.status} /></div>
            <div className="kv__key">Score</div>
            <div className="kv__value">{call.lead.score ?? '—'}</div>
          </div>
        )}
      </div>
    </section>
  )
}

function CrmPanel({ call, timezone }) {
  const syncs = call.crm_syncs ?? []
  if (!syncs.length) return null

  return (
    <section className="card" aria-label="CRM sync">
      <div className="card__header"><h3>CRM sync</h3></div>
      <div className="card__body">
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Provider</th>
                <th scope="col">Status</th>
                <th scope="col" className="table__numeric">Attempts</th>
                <th scope="col">Synced</th>
                <th scope="col">Detail</th>
              </tr>
            </thead>
            <tbody>
              {syncs.map((sync, index) => (
                <tr key={`${sync.provider}-${index}`}>
                  <td>{humanise(sync.provider)}</td>
                  <td><StatusBadge status={sync.status} /></td>
                  <td className="table__numeric">{sync.attempt_count}</td>
                  <td>
                    {sync.synced_at ? formatDateTime(sync.synced_at, timezone) : '—'}
                  </td>
                  {/*
                    Already scrubbed of anything credential-shaped by
                    `errors.safe_message` before it was stored (STEP 5).
                    Rendered as text regardless.
                  */}
                  <td className="muted">{sync.last_error || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  )
}