/**
 * Shared UI primitives.
 *
 * Requirement 21 asks every page for loading, empty, permission-denied, 404,
 * validation, server-error and retry states. Written once here so a page
 * cannot ship without them by forgetting.
 *
 * Nothing in this file uses `dangerouslySetInnerHTML`. React escapes text by
 * default, and that default is the XSS defence for every piece of
 * caller-supplied content in the product — transcripts, lead names, document
 * titles, audit details.
 */
import { useEffect, useRef } from 'react'

/* --------------------------------------------------------------- states --- */

export function Loading({ label = 'Loading…', rows = 3 }) {
  return (
    <div className="card__body" role="status" aria-live="polite">
      <span className="sr-only">{label}</span>
      <div className="stack" aria-hidden="true">
        {Array.from({ length: rows }).map((_, index) => (
          <div
            key={index}
            className="skeleton"
            style={{ width: `${100 - index * 12}%` }}
          />
        ))}
      </div>
    </div>
  )
}

export function EmptyState({ icon = '○', title, description, action }) {
  return (
    <div className="state">
      <div className="state__icon" aria-hidden="true">{icon}</div>
      <div className="state__title">{title}</div>
      {description && <div style={{ maxWidth: '46ch' }}>{description}</div>}
      {action}
    </div>
  )
}

/**
 * Render an `ApiError` as something a person can act on.
 *
 * A 403 is not a failure to retry — it is a statement about the account — so
 * it gets its own copy and no retry button. A 5xx or a network drop does.
 */
export function ErrorState({ error, onRetry, resource = 'this' }) {
  if (!error) return null

  if (error.status === 403) {
    return (
      <EmptyState
        icon="🔒"
        title="You do not have access to this"
        description={`Your role does not include permission to view ${resource}. Ask an owner or admin if you need it.`}
      />
    )
  }
  if (error.status === 404) {
    return (
      <EmptyState
        icon="?"
        title="Not found"
        description="That item does not exist, or it belongs to another account."
      />
    )
  }
  return (
    <div className="state">
      <div className="state__icon" aria-hidden="true">!</div>
      <div className="state__title">Could not load {resource}</div>
      <div style={{ maxWidth: '46ch' }}>{error.message}</div>
      {onRetry && error.retryable && (
        <button type="button" className="btn" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  )
}

export function Alert({ tone = 'info', children, onDismiss }) {
  return (
    <div className={`alert alert--${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
      <div style={{ flex: 1 }}>{children}</div>
      {onDismiss && (
        <button
          type="button"
          className="btn--link"
          onClick={onDismiss}
          aria-label="Dismiss"
        >
          ×
        </button>
      )}
    </div>
  )
}

/**
 * The wrapper every page body uses.
 *
 * Collapses the four-way loading/error/empty/data branch into one place, so a
 * page cannot accidentally render a blank div while loading.
 */
export function AsyncSection({
  loading, error, isEmpty, onRetry, resource, empty, children,
}) {
  if (loading) return <Loading label={`Loading ${resource ?? 'data'}…`} />
  if (error) return <ErrorState error={error} onRetry={onRetry} resource={resource} />
  if (isEmpty) return empty ?? <EmptyState title="Nothing here yet" />
  return children
}

/* --------------------------------------------------------------- badges --- */

const TONE_BY_STATUS = {
  completed: 'ok', confirmed: 'ok', active: 'ok', ready: 'ok', synced: 'ok',
  paid: 'ok', connected: 'ok', qualified: 'ok', booked: 'ok',
  in_progress: 'info', processing: 'info', pending: 'info', trialing: 'info',
  ringing: 'info', uploaded: 'info', rescheduled: 'info', canceling: 'warn',
  no_answer: 'warn', past_due: 'warn', missed: 'warn', archived: 'muted',
  no_show: 'warn', open: 'warn', draft: 'muted', incomplete: 'warn',
  failed: 'danger', cancelled: 'danger', canceled: 'danger',
  permanent_failure: 'danger', do_not_call: 'danger', uncollectible: 'danger',
  incomplete_expired: 'danger', void: 'muted',
}

/**
 * A status pill.
 *
 * The label is always rendered as text. Requirement 27 forbids colour as the
 * only signal, and the old dashboard used a bare coloured `<span>` — invisible
 * to a screen reader and to anyone with a colour-vision deficiency.
 */
export function StatusBadge({ status, label }) {
  if (!status) return <span className="muted">—</span>
  const key = String(status).toLowerCase()
  const tone = TONE_BY_STATUS[key] ?? 'muted'
  const text = label ?? key.replace(/_/g, ' ')
  return <span className={`badge badge--${tone}`}>{text}</span>
}

export function BooleanBadge({ value, yes = 'Yes', no = 'No', tone = 'ok' }) {
  return value
    ? <span className={`badge badge--${tone}`}>{yes}</span>
    : <span className="muted">{no}</span>
}

/* --------------------------------------------------------------- tables --- */

export function DataTable({ columns, rows, keyOf, caption, emptyMessage }) {
  if (!rows?.length) {
    return <EmptyState title={emptyMessage ?? 'No results'} />
  }
  return (
    <div className="table-wrap">
      <table className="table">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr>
            {columns.map((column) => (
              // `scope` is what lets a screen reader associate a cell with
              // its header. The old table had none.
              <th
                key={column.key}
                scope="col"
                className={column.numeric ? 'table__numeric' : undefined}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={keyOf(row)}>
              {columns.map((column) => (
                <td
                  key={column.key}
                  className={column.numeric ? 'table__numeric' : undefined}
                  data-label={column.header}
                >
                  {column.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function Pager({ page, pageCount, total, limit, onPage, label = 'results' }) {
  const from = total === 0 ? 0 : (page - 1) * limit + 1
  const to = Math.min(page * limit, total)

  return (
    <nav className="pager" aria-label="Pagination">
      <div className="pager__status" aria-live="polite">
        {total === 0
          ? `No ${label}`
          : `${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()} ${label}`}
      </div>
      <div className="row">
        <button
          type="button" className="btn btn--small"
          onClick={() => onPage(page - 1)} disabled={page <= 1}
        >
          Previous
        </button>
        <span className="pager__status">Page {page} of {pageCount}</span>
        <button
          type="button" className="btn btn--small"
          onClick={() => onPage(page + 1)} disabled={page >= pageCount}
        >
          Next
        </button>
      </div>
    </nav>
  )
}

/* --------------------------------------------------------------- dialog --- */

/**
 * A modal with real focus management.
 *
 * Focus moves in on open, is trapped while open, and returns to the trigger on
 * close. Escape dismisses. Without this a keyboard user tabs out of the dialog
 * into the page behind it and cannot find their way back.
 */
export function Dialog({ open, title, onClose, children, footer }) {
  const panel = useRef(null)
  const previouslyFocused = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    previouslyFocused.current = document.activeElement
    const node = panel.current
    const focusable = node?.querySelectorAll(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
    )
    focusable?.[0]?.focus()

    const onKeyDown = (event) => {
      if (event.key === 'Escape') {
        onClose()
        return
      }
      if (event.key !== 'Tab' || !focusable?.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      previouslyFocused.current?.focus?.()
    }
  }, [open, onClose])

  if (!open) return null

  return (
    <div
      className="dialog-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        ref={panel}
      >
        <div className="dialog__header">
          <h2>{title}</h2>
          <button
            type="button" className="btn btn--small"
            onClick={onClose} aria-label="Close dialog"
          >
            ×
          </button>
        </div>
        <div className="dialog__body">{children}</div>
        {footer && <div className="dialog__footer">{footer}</div>}
      </div>
    </div>
  )
}

/* ---------------------------------------------------------------- forms --- */

export function Field({ label, hint, error, htmlFor, children }) {
  return (
    <div className="field">
      <label className="field__label" htmlFor={htmlFor}>{label}</label>
      {children}
      {hint && <div className="field__hint">{hint}</div>}
      {error && <div className="field__error" role="alert">{error}</div>}
    </div>
  )
}

/* ---------------------------------------------------------------- stats --- */

export function StatCard({ label, value, hint, tone }) {
  return (
    <div className="card">
      <div className="card__body">
        <div className="stat__label">{label}</div>
        <div className="stat__value" style={tone ? { color: `var(--${tone})` } : undefined}>
          {value}
        </div>
        {hint && <div className="stat__hint">{hint}</div>}
      </div>
    </div>
  )
}

/** A card explaining that a figure is genuinely unavailable, not zero. */
export function UnavailableCard({ label, reason }) {
  return (
    <div className="card">
      <div className="card__body">
        <div className="stat__label">{label}</div>
        <div className="stat__value muted" style={{ fontSize: 18 }}>Not available</div>
        <div className="stat__hint">{reason}</div>
      </div>
    </div>
  )
}
