/**
 * Audit log.
 *
 * One endpoint backs this page:
 *
 *   GET /api/team/audit?limit=(1..500, default 100)   ->  bare array
 *
 * `limit` is its **only** parameter. There is no `action`, `actor`, `from`,
 * `to`, `cursor` or `offset` filter, and no total count -- unknown query
 * parameters are silently ignored by FastAPI, so inventing one would not
 * error, it would just quietly do nothing. Every filter on this page is
 * therefore **client-side over the rows already fetched**, and the UI says so
 * rather than implying the server is filtering.
 *
 * Because there is no count and no cursor, "showing N events" is stated as
 * exactly that -- the number of rows returned -- never as a total. When the
 * response length equals the requested limit the list is probably truncated,
 * so the page says it may be and offers a larger limit.
 *
 * ## View-only
 *
 * An audit log you can edit is not an audit log. There is no mutation route
 * and this page adds no control that could imply one -- no delete, no
 * annotate, and no Export button, because no export endpoint exists.
 *
 * ## Redaction
 *
 * `AuditLog.detail` is free-form JSON. Every writer in the codebase is
 * disciplined (`{"reason": "bad_password"}`, `{"from": "viewer", "to":
 * "agent"}`, `{"missing": [...], "path": "..."}`, `{"provider": "stripe"}`),
 * and the model docstring promises no secrets. But "the column is free-form"
 * plus "a future writer could be careless" is exactly the case for
 * defence in depth, so `redact()` masks by key before rendering. It is
 * conservative and key-based: operational context survives, anything whose
 * key looks credential-shaped is replaced with a marker. The frontend cannot
 * fix a backend leak -- it can only avoid painting it on screen.
 */
import { useMemo, useState } from 'react'

import {
  Alert, AsyncSection, DataTable, EmptyState, StatCard,
} from '../components/ui'
import { listAudit, listUsers } from '../lib/api'
import { formatDateTime, formatNumber, humanise } from '../lib/format'
import { useApi } from '../lib/hooks'
import { PERMISSIONS as P } from '../lib/permissions'

/** The server's own ceiling: `Query(100, le=500)`. Asking for more is a 422. */
const MAX_LIMIT = 500
const LIMITS = [50, 100, 250, 500]

/**
 * Keys whose values must never be rendered.
 *
 * Matched case-insensitively against the whole key path, so a nested
 * `{"config": {"api_key": ...}}` is caught too. Deliberately broad: a false
 * positive costs one hidden debug value, a false negative prints a secret.
 */
const SECRET_KEY = new RegExp([
  'secret', 'token', 'password', 'passwd', 'credential', 'api[-_]?key',
  'apikey', 'private', 'signature', 'signing', 'authorization', 'auth[-_]?header',
  'session[-_]?id', 'cookie', 'encryption', 'cipher', 'salt', 'hash',
  'client[-_]?secret', 'access[-_]?key', 'bearer',
].join('|'), 'i')

/** Values that look like a credential regardless of their key. */
const SECRET_VALUE = [
  /^sk_(live|test)_[A-Za-z0-9]+/,        // Stripe secret key
  /^whsec_[A-Za-z0-9]+/,                 // Stripe webhook secret
  /^rk_(live|test)_[A-Za-z0-9]+/,        // Stripe restricted key
  /^ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\./,  // JWT
  /^\$2[aby]\$\d{2}\$[./A-Za-z0-9]{20,}/, // bcrypt hash
  /^(ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}/,  // GitHub token
  /^xox[baprs]-[A-Za-z0-9-]{10,}/,       // Slack token
  /^AKIA[0-9A-Z]{16}$/,                  // AWS access key id
]

const REDACTED = '[redacted]'

/**
 * Recursively mask secret-looking entries in an arbitrary JSON value.
 *
 * Depth- and size-capped: `detail` is attacker-influenceable in principle
 * (a crafted request path lands in an AUTHZ_DENIED row), so a pathological
 * structure must not be able to hang the render.
 */
export function redact(value, key = '', depth = 0) {
  if (depth > 6) return '[too deep]'

  if (key && SECRET_KEY.test(key)) return REDACTED

  if (typeof value === 'string') {
    return SECRET_VALUE.some((rx) => rx.test(value)) ? REDACTED : value
  }
  if (value === null || typeof value !== 'object') return value

  if (Array.isArray(value)) {
    return value.slice(0, 50).map((item) => redact(item, key, depth + 1))
  }
  return Object.fromEntries(
    Object.entries(value).slice(0, 50)
      .map(([k, v]) => [k, redact(v, k, depth + 1)])
  )
}

/**
 * How each real `AuditAction` should read.
 *
 * Tone is driven only by the action itself -- a real field. There is no risk
 * score, no severity heuristic, and nothing inferred about how "dangerous"
 * an event was.
 */
const ACTION_TONE = {
  login_failure: 'warn',
  authz_denied: 'danger',
  user_deactivated: 'warn',
  role_changed: 'warn',
  password_changed: 'warn',
  billing_payment_failed: 'danger',
  user_created: 'info',
  user_reactivated: 'info',
  login_success: 'ok',
  logout: 'muted',
  token_refresh: 'muted',
}

/** Coarse grouping for the filter, built from the action strings themselves. */
function categoryOf(action) {
  if (action.startsWith('billing_')) return 'Billing'
  if (action.startsWith('integration_') || action.startsWith('calendar_')) {
    return 'Integrations'
  }
  if (action.startsWith('appointment_')) return 'Appointments'
  if (action.startsWith('user_') || action === 'role_changed') return 'Team'
  return 'Sign-in and access'
}

export default function Audit({ me, can }) {
  const timezone = me.tenant.timezone
  const [limit, setLimit] = useState(100)
  const [search, setSearch] = useState('')
  const [action, setAction] = useState('')
  const [expanded, setExpanded] = useState(() => new Set())

  const events = useApi(() => listAudit({ limit }), [limit])

  // Everyone with audit:read also has user:read in the real RBAC table, so
  // the target UUID can be shown as a person. If the call is ever denied the
  // page still works -- it just shows the raw id.
  const mayReadUsers = can(P.USER_READ)
  const users = useApi(() => (mayReadUsers ? listUsers() : Promise.resolve([])), [])

  const emailById = useMemo(() => {
    const map = new Map()
    for (const user of users.data ?? []) map.set(user.id, user.email)
    return map
  }, [users.data])

  const rows = events.data ?? []

  // The response carries no total, so a full page is a hint of truncation,
  // not proof of it. Say exactly that.
  const maybeTruncated = rows.length === limit

  const actions = useMemo(
    () => [...new Set(rows.map((row) => row.action))].sort(),
    [rows]
  )

  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase()
    return rows.filter((row) => {
      if (action && row.action !== action) return false
      if (!needle) return true
      const target = emailById.get(row.target_user_id) ?? row.target_user_id ?? ''
      return [row.action, row.actor_email, row.ip_address, target,
        JSON.stringify(redact(row.detail))]
        .join(' ').toLowerCase().includes(needle)
    })
  }, [rows, action, search, emailById])

  const toggle = (id) => setExpanded((previous) => {
    const next = new Set(previous)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

  const columns = useMemo(() => [
    {
      key: 'when', header: 'When',
      render: (row) => (
        <span style={{ whiteSpace: 'nowrap' }}>
          {formatDateTime(row.created_at, timezone)}
        </span>
      ),
    },
    {
      key: 'action', header: 'Event',
      render: (row) => (
        <span className={`badge badge--${ACTION_TONE[row.action] ?? 'muted'}`}>
          {humanise(row.action)}
        </span>
      ),
    },
    {
      key: 'actor', header: 'Who',
      render: (row) => (row.actor_email
        // Recorded even for a failed login, so it is an attempted identity,
        // not a proven one.
        ? <span style={{ overflowWrap: 'anywhere' }}>{row.actor_email}</span>
        : <span className="muted">System</span>),
    },
    {
      key: 'target', header: 'Affected',
      render: (row) => {
        if (!row.target_user_id) return <span className="muted">—</span>
        const email = emailById.get(row.target_user_id)
        return email
          ? <span style={{ overflowWrap: 'anywhere' }}>{email}</span>
          : (
            <span className="muted" title={row.target_user_id}>
              {/* A deleted or foreign id stays a raw id rather than a guess. */}
              {row.target_user_id.slice(0, 8)}…
            </span>
          )
      },
    },
    {
      key: 'ip', header: 'IP address',
      render: (row) => (row.ip_address
        ? <code style={{ fontSize: 12 }}>{row.ip_address}</code>
        : <span className="muted">—</span>),
    },
    {
      key: 'detail', header: 'Details',
      render: (row) => {
        const detail = row.detail
        const hasDetail = detail && Object.keys(detail).length > 0
        if (!hasDetail) return <span className="muted">—</span>
        const isOpen = expanded.has(row.id)
        return (
          <div>
            <button
              type="button"
              className="btn btn--link btn--small"
              aria-expanded={isOpen}
              onClick={() => toggle(row.id)}
            >
              {isOpen ? 'Hide' : 'Show'}
            </button>
            {/* Expanded in place: `DataTable` has no expanded-row slot, and
                adding one would change a component five other pages use. */}
            {isOpen && <Detail detail={detail} />}
          </div>
        )
      },
    },
  ], [timezone, emailById, expanded])

  return (
    <>
      <div className="page__header">
        <div>
          <h1>Audit log</h1>
          <p className="page__description">
            Security-relevant events for this workspace: sign-ins, permission
            denials, team changes and billing actions. Records are written by
            the server and cannot be edited or removed.
          </p>
        </div>
      </div>

      <div className="grid grid--stats">
        {/* Never called a total: the endpoint returns no count. */}
        <StatCard
          label="Events shown"
          value={formatNumber(rows.length)}
          hint={`Most recent ${limit}`}
        />
        <StatCard
          label="Sign-in failures"
          value={formatNumber(rows.filter((r) => r.action === 'login_failure').length)}
          hint="In the events shown"
        />
        <StatCard
          label="Permission denials"
          value={formatNumber(rows.filter((r) => r.action === 'authz_denied').length)}
          hint="In the events shown"
        />
      </div>

      <section className="card" aria-label="Audit events">
        <div className="card__header">
          <div>
            <h3>Events</h3>
            <p className="muted" style={{ margin: '2px 0 0', fontSize: 13 }}>
              Newest first, in {timezone}.
            </p>
          </div>
          <div className="row" style={{ gap: 12, flexWrap: 'wrap' }}>
            <label className="sr-only" htmlFor="audit-search">Search events</label>
            <input
              id="audit-search"
              className="input"
              type="search"
              placeholder="Search events"
              style={{ width: 'auto' }}
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />

            <label className="sr-only" htmlFor="audit-action">Filter by event</label>
            <select
              id="audit-action"
              className="select"
              style={{ width: 'auto' }}
              value={action}
              onChange={(event) => setAction(event.target.value)}
            >
              <option value="">All events</option>
              {actions.map((value) => (
                <option key={value} value={value}>
                  {humanise(value)} · {categoryOf(value)}
                </option>
              ))}
            </select>

            <label className="sr-only" htmlFor="audit-limit">Events to load</label>
            <select
              id="audit-limit"
              className="select"
              style={{ width: 'auto' }}
              value={limit}
              onChange={(event) => setLimit(Number(event.target.value))}
            >
              {LIMITS.map((value) => (
                <option key={value} value={value}>Load {value}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="card__body">
          {/* Honest about where the work happens. */}
          <p className="muted" style={{ fontSize: 12, marginTop: 0 }}>
            The server returns the most recent {limit} events; searching and
            filtering happen in your browser across those events only.
          </p>

          {maybeTruncated && limit < MAX_LIMIT && (
            <Alert tone="info">
              There may be older events than the {limit} shown. Load more to
              look further back.
            </Alert>
          )}

          <AsyncSection
            loading={events.loading}
            error={events.error}
            onRetry={events.reload}
            resource="the audit log"
            isEmpty={Boolean(events.data) && rows.length === 0}
            empty={
              <EmptyState
                icon="?"
                title="No audit events yet"
                description="Sign-ins, team changes and billing actions will appear here as they happen."
              />
            }
          >
            {rows.length > 0 && (
              visible.length > 0 ? (
                <DataTable
                  caption="Audit events"
                  columns={columns}
                  rows={visible}
                  keyOf={(row) => row.id}
                />
              ) : (
                <EmptyState
                  icon="?"
                  title="No matching events"
                  description="Try a different search or event type."
                />
              )
            )}
          </AsyncSection>
        </div>
      </section>
    </>
  )
}

/**
 * The `detail` blob, redacted and rendered as text.
 *
 * `JSON.stringify` output goes in as a React child, so it is escaped -- a
 * detail value containing markup is displayed, never parsed.
 */
function Detail({ detail }) {
  const safe = useMemo(() => redact(detail), [detail])
  return (
    <div className="kv" style={{ gridTemplateColumns: 'minmax(120px,200px) 1fr' }}>
      {Object.entries(safe).map(([key, value]) => (
        <Row key={key} label={key} value={value} />
      ))}
    </div>
  )
}

function Row({ label, value }) {
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  return (
    <>
      <dt>{humanise(label)}</dt>
      <dd style={{ overflowWrap: 'anywhere' }}>
        <code style={{ fontSize: 12 }}>{text}</code>
      </dd>
    </>
  )
}