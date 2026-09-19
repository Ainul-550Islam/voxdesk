/**
 * Knowledge base.
 *
 * Everything here is backed by a route that already exists in
 * `app/api/knowledge_routes.py`. Nothing is computed in the browser that the
 * server already computes, and no control appears for an operation the
 * backend cannot perform:
 *
 *   GET    /api/knowledge/stats                     the numbers
 *   GET    /api/knowledge/documents?status&limit&offset   list, server-filtered
 *   POST   /api/knowledge/documents                 upload (202, async)
 *   POST   /api/knowledge/documents/{id}/reindex    retry / rebuild
 *   POST   /api/knowledge/documents/{id}/restore    un-archive
 *   DELETE /api/knowledge/documents/{id}[?hard]     archive, or purge
 *
 * There is no rename and no edit, so there is no rename or edit button.
 *
 * **Ingestion is asynchronous.** Upload returns 202 with the document in
 * `uploaded`; the worker moves it to `processing` then `ready` or `failed`.
 * The API offers no push channel, so the page polls while anything is still
 * in flight and stops as soon as nothing is -- a spinner that never resolves
 * is worse than a stale row.
 *
 * **Every string here is untrusted.** Filenames, titles and extractor error
 * messages are all attacker-influenceable (anyone who can upload can name a
 * file `<img onerror=...>`). All of it is rendered as React children, never
 * as HTML, and document *contents* are never rendered at all.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'

import {
  Alert, AsyncSection, DataTable, Dialog, EmptyState, Field, Pager, StatCard,
  StatusBadge,
} from '../components/ui'
import {
  deleteDocument, knowledgeStats, listDocuments, reindexDocument,
  restoreDocument, uploadDocument,
} from '../lib/api'
import { formatDateTime, formatNumber } from '../lib/format'
import { useAction, useApi, usePagination } from '../lib/hooks'
import { PERMISSIONS as P } from '../lib/permissions'

/** The backend's `DocumentStatus` enum, verbatim. */
const STATUSES = [
  ['', 'Any status'],
  ['uploaded', 'Uploaded'],
  ['processing', 'Processing'],
  ['ready', 'Ready'],
  ['failed', 'Failed'],
  ['archived', 'Archived'],
]

/** Statuses that mean the worker has not finished yet. */
const IN_FLIGHT = new Set(['uploaded', 'processing'])

const POLL_MS = 4000

/** Bytes as something a person reads. The API returns raw `file_size`. */
function formatBytes(bytes) {
  const value = Number(bytes) || 0
  if (value === 0) return '—'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(0)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

export default function Knowledge({ me, can }) {
  const timezone = me.tenant.timezone
  const mayWrite = can(P.KNOWLEDGE_WRITE)
  const mayPurge = can(P.KNOWLEDGE_DELETE)

  const [status, setStatus] = useState('')
  const [flash, setFlash] = useState(null)
  const [confirming, setConfirming] = useState(null)
  const pager = usePagination(25)

  // Filtering resets to page 1: staying on page 4 of a set that now has one
  // shows an empty table and reads as a bug.
  useEffect(() => {
    pager.reset()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status])

  const params = {
    status: status || undefined,
    limit: pager.limit,
    offset: pager.offset,
  }
  const key = JSON.stringify(params)

  const stats = useApi(() => knowledgeStats(), [])
  const list = useApi(() => listDocuments(params), [key])

  const documents = list.data?.documents ?? []
  const total = list.data?.total ?? 0
  const limits = list.data?.limits ?? null

  const refresh = useCallback(() => {
    list.reload()
    stats.reload()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [list.reload, stats.reload])

  // Poll only while something is actually being processed. `useRef` holds the
  // latest refresh so the interval does not restart on every render.
  const refreshRef = useRef(refresh)
  refreshRef.current = refresh
  const pending = documents.some((doc) => IN_FLIGHT.has(doc.status))

  useEffect(() => {
    if (!pending) return undefined
    const timer = setInterval(() => refreshRef.current(), POLL_MS)
    return () => clearInterval(timer)
  }, [pending])

  const reindex = useAction((id) => reindexDocument(id), {
    onSuccess: () => { setFlash('Reindexing started.'); refresh() },
  })
  const restore = useAction((id) => restoreDocument(id), {
    onSuccess: () => { setFlash('Document restored.'); refresh() },
  })
  const remove = useAction(
    ({ id, hard }) => deleteDocument(id, hard),
    {
      onSuccess: (result) => {
        setConfirming(null)
        setFlash(
          result?.status === 'deleted'
            ? 'Document permanently deleted.'
            : 'Document archived. It is no longer used to answer calls.'
        )
        refresh()
      },
    }
  )

  const busy = reindex.pending || restore.pending || remove.pending
  const actionError = reindex.error || restore.error || remove.error

  const columns = useMemo(() => [
    {
      key: 'title', header: 'Document',
      render: (doc) => (
        <div>
          {/* Tenant-supplied. React escapes it; never dangerouslySetInnerHTML. */}
          <div style={{ fontWeight: 550 }}>{doc.title}</div>
          {doc.original_filename && doc.original_filename !== doc.title && (
            <div className="muted" style={{ fontSize: 12 }}>
              {doc.original_filename}
            </div>
          )}
        </div>
      ),
    },
    {
      key: 'type', header: 'Type',
      render: (doc) => doc.file_type
        ? <span className="badge badge--muted">{doc.file_type}</span>
        : <span className="muted">—</span>,
    },
    {
      key: 'status', header: 'Status',
      render: (doc) => (
        <div className="row" style={{ gap: 6 }}>
          <StatusBadge status={doc.status} />
          {doc.is_searchable && (
            <span className="badge badge--ok">Searchable</span>
          )}
        </div>
      ),
    },
    {
      key: 'chunks', header: 'Chunks', numeric: true,
      render: (doc) => formatNumber(doc.chunk_count),
    },
    {
      key: 'size', header: 'Size', numeric: true,
      render: (doc) => formatBytes(doc.file_size),
    },
    {
      key: 'created', header: 'Uploaded',
      // Tenant timezone, not the browser's.
      render: (doc) => formatDateTime(doc.created_at, timezone),
    },
    {
      key: 'actions', header: 'Actions',
      render: (doc) => (
        <RowActions
          doc={doc}
          mayWrite={mayWrite}
          mayPurge={mayPurge}
          busy={busy}
          onReindex={() => { setFlash(null); reindex.run(doc.id) }}
          onRestore={() => { setFlash(null); restore.run(doc.id) }}
          onDelete={(hard) => { setFlash(null); setConfirming({ doc, hard }) }}
        />
      ),
    },
  ], [timezone, mayWrite, mayPurge, busy, reindex, restore])

  return (
    <>
      <div className="page__header">
        <div>
          <h1>Knowledge</h1>
          <p className="page__description">
            The documents the agent uses to answer questions on a call.
          </p>
        </div>
      </div>

      {flash && (
        <Alert tone="ok" onDismiss={() => setFlash(null)}>{flash}</Alert>
      )}
      {actionError && (
        <Alert
          tone="error"
          onDismiss={() => {
            reindex.clearError(); restore.clearError(); remove.clearError()
          }}
        >
          {actionError.message}
        </Alert>
      )}

      <Stats stats={stats} />

      {mayWrite && (
        <Upload
          limits={limits}
          onUploaded={(message) => { setFlash(message); refresh() }}
        />
      )}

      <section className="card" aria-label="Documents">
        <div className="card__header">
          <h3>Documents</h3>
          <div className="row">
            <label className="sr-only" htmlFor="doc-status">
              Filter by status
            </label>
            <select
              id="doc-status"
              className="select"
              style={{ width: 'auto' }}
              value={status}
              onChange={(event) => setStatus(event.target.value)}
            >
              {STATUSES.map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
          </div>
        </div>

        <div className="card__body">
          {pending && (
            <p className="muted" style={{ marginTop: 0 }} aria-live="polite">
              Some documents are still being processed. This list refreshes
              automatically.
            </p>
          )}

          <AsyncSection
            loading={list.loading}
            error={list.error}
            onRetry={list.reload}
            resource="documents"
            isEmpty={list.data && total === 0}
            empty={
              status ? (
                <EmptyState
                  icon="▣"
                  title={`No ${status} documents`}
                  description="Try a different status filter."
                />
              ) : (
                <EmptyState
                  icon="▣"
                  title="No documents yet"
                  description={
                    mayWrite
                      ? 'Upload a PDF, Word document, spreadsheet or text file and the agent will start using it to answer questions.'
                      : 'Nothing has been uploaded to this knowledge base yet.'
                  }
                />
              )
            }
          >
            {list.data && total > 0 && (
              <>
                <DataTable
                  caption="Knowledge documents"
                  columns={columns}
                  rows={documents}
                  keyOf={(doc) => doc.id}
                />
                <Pager
                  page={pager.page}
                  pageCount={pager.pageCount(total)}
                  total={total}
                  limit={pager.limit}
                  onPage={pager.setPage}
                  label="documents"
                />
              </>
            )}
          </AsyncSection>

          <FailureDetails documents={documents} />
        </div>
      </section>

      <ConfirmDialog
        confirming={confirming}
        pending={remove.pending}
        onCancel={() => setConfirming(null)}
        onConfirm={() =>
          remove.run({ id: confirming.doc.id, hard: confirming.hard })}
      />
    </>
  )
}

/* ---------------------------------------------------------------- stats --- */

function Stats({ stats }) {
  return (
    <AsyncSection
      loading={stats.loading}
      error={stats.error}
      onRetry={stats.reload}
      resource="the knowledge statistics"
    >
      {stats.data && (
        <>
          <div className="grid grid--stats">
            <StatCard
              label="Documents"
              value={formatNumber(stats.data.total_documents)}
            />
            <StatCard
              label="Ready"
              value={formatNumber(stats.data.documents?.ready ?? 0)}
              hint="Searchable by the agent"
            />
            <StatCard
              label="Processing"
              value={formatNumber(
                (stats.data.documents?.uploaded ?? 0)
                + (stats.data.documents?.processing ?? 0)
              )}
            />
            <StatCard
              label="Failed"
              value={formatNumber(stats.data.documents?.failed ?? 0)}
              tone={stats.data.documents?.failed ? 'danger' : undefined}
            />
            <StatCard
              label="Searchable chunks"
              value={formatNumber(stats.data.searchable_chunks)}
              hint={`${formatNumber(stats.data.total_chunks)} total`}
            />
          </div>

          {/*
            Model name and vector width only. The embedding provider's API key
            lives in server settings and is never part of this response.
          */}
          <p className="muted" style={{ fontSize: 12, marginTop: -4 }}>
            Embedding model <strong>{stats.data.embedding_model}</strong>
            {stats.data.embedding_dimensions
              ? ` · ${formatNumber(stats.data.embedding_dimensions)} dimensions`
              : ''}
          </p>
        </>
      )}
    </AsyncSection>
  )
}

/* --------------------------------------------------------------- upload --- */

function Upload({ limits, onUploaded }) {
  const inputRef = useRef(null)
  const [file, setFile] = useState(null)
  const [title, setTitle] = useState('')
  const [localError, setLocalError] = useState(null)

  const types = limits?.supported_types ?? []
  const maxMb = limits?.max_file_mb ?? null
  const accept = types.map((type) => `.${type}`).join(',')

  const upload = useAction(
    () => uploadDocument(file, title.trim()),
    {
      onSuccess: (document) => {
        // A duplicate returns 200 with the existing row rather than 202. The
        // server distinguishes them; say which happened instead of claiming a
        // new upload that did not occur.
        const already = document?.status === 'ready' && document?.chunk_count > 0
        onUploaded(
          already
            ? 'That file is already in the knowledge base.'
            : 'Upload accepted. Processing starts automatically.'
        )
        setFile(null)
        setTitle('')
        setLocalError(null)
        if (inputRef.current) inputRef.current.value = ''
      },
    }
  )

  /**
   * Client-side checks for fast feedback only.
   *
   * The backend sniffs magic bytes and re-checks the size; a renamed
   * executable is caught there, not here. This exists so the common mistake
   * does not cost a round trip, never as the security boundary.
   */
  const validate = (candidate) => {
    if (!candidate) return 'Choose a file first.'
    const extension = candidate.name.includes('.')
      ? candidate.name.split('.').pop().toLowerCase()
      : ''
    if (types.length && !types.includes(extension)) {
      return `That file type is not supported. Allowed: ${types.join(', ')}.`
    }
    if (maxMb && candidate.size > maxMb * 1024 * 1024) {
      return `That file is larger than the ${maxMb} MB limit.`
    }
    return null
  }

  const onSubmit = (event) => {
    event.preventDefault()
    const problem = validate(file)
    if (problem) { setLocalError(problem); return }
    setLocalError(null)
    upload.run()
  }

  return (
    <section className="card" aria-label="Upload a document">
      <div className="card__header"><h3>Add a document</h3></div>
      <div className="card__body">
        <form className="stack" onSubmit={onSubmit} noValidate>
          <div className="grid grid--halves">
            <Field
              label="File"
              htmlFor="doc-file"
              hint={
                types.length
                  ? `${types.join(', ')}${maxMb ? ` · up to ${maxMb} MB` : ''}`
                  : 'Choose a document to upload.'
              }
              error={localError}
            >
              <input
                id="doc-file"
                ref={inputRef}
                className="input"
                type="file"
                accept={accept || undefined}
                disabled={upload.pending}
                onChange={(event) => {
                  const chosen = event.target.files?.[0] ?? null
                  setFile(chosen)
                  setLocalError(chosen ? validate(chosen) : null)
                }}
              />
            </Field>

            <Field
              label="Title (optional)"
              htmlFor="doc-title"
              hint="Defaults to the filename."
            >
              <input
                id="doc-title"
                className="input"
                type="text"
                value={title}
                maxLength={200}
                disabled={upload.pending}
                onChange={(event) => setTitle(event.target.value)}
              />
            </Field>
          </div>

          {upload.error && (
            <Alert tone="error" onDismiss={upload.clearError}>
              {upload.error.message}
            </Alert>
          )}

          <div className="toolbar">
            <button
              type="submit"
              className="btn btn--primary"
              disabled={!file || upload.pending || Boolean(localError)}
            >
              {upload.pending ? 'Uploading…' : 'Upload'}
            </button>
            <span className="muted" style={{ fontSize: 12 }} aria-live="polite">
              {upload.pending
                ? 'Sending the file…'
                : 'Indexing runs in the background after upload.'}
            </span>
          </div>
        </form>
      </div>
    </section>
  )
}

/* -------------------------------------------------------------- actions --- */

function RowActions({
  doc, mayWrite, mayPurge, busy, onReindex, onRestore, onDelete,
}) {
  if (!mayWrite) return <span className="muted">—</span>

  const archived = doc.status === 'archived'
  // The backend refuses a reindex of an archived document (409) and treats a
  // reindex of one already processing as a no-op, so neither gets a button.
  const canReindex = !archived && doc.status !== 'processing'

  return (
    <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
      {archived ? (
        <button
          type="button" className="btn btn--small"
          disabled={busy} onClick={onRestore}
        >
          Restore
        </button>
      ) : (
        <>
          {canReindex && (
            <button
              type="button" className="btn btn--small"
              disabled={busy} onClick={onReindex}
            >
              {doc.status === 'failed' ? 'Retry' : 'Reindex'}
            </button>
          )}
          <button
            type="button" className="btn btn--small"
            disabled={busy} onClick={() => onDelete(false)}
          >
            Archive
          </button>
        </>
      )}
      {mayPurge && (
        <button
          type="button" className="btn btn--small btn--danger"
          disabled={busy} onClick={() => onDelete(true)}
        >
          Delete
        </button>
      )}
    </div>
  )
}

function ConfirmDialog({ confirming, pending, onCancel, onConfirm }) {
  const hard = confirming?.hard
  return (
    <Dialog
      open={Boolean(confirming)}
      title={hard ? 'Delete permanently?' : 'Archive this document?'}
      onClose={onCancel}
      footer={
        <>
          <button type="button" className="btn" onClick={onCancel} disabled={pending}>
            Cancel
          </button>
          <button
            type="button"
            className={hard ? 'btn btn--danger' : 'btn btn--primary'}
            onClick={onConfirm}
            disabled={pending}
          >
            {pending
              ? 'Working…'
              : hard ? 'Delete permanently' : 'Archive'}
          </button>
        </>
      }
    >
      {confirming && (
        <p style={{ margin: 0 }}>
          {hard ? (
            <>
              <strong>{confirming.doc.title}</strong> and its extracted text
              will be destroyed. This cannot be undone, and the record will no
              longer be available for audits.
            </>
          ) : (
            <>
              <strong>{confirming.doc.title}</strong> will stop being used to
              answer calls immediately. It is kept, and you can restore it
              later.
            </>
          )}
        </p>
      )}
    </Dialog>
  )
}

/* ------------------------------------------------------------- failures --- */

function FailureDetails({ documents }) {
  const failed = documents.filter(
    (doc) => doc.status === 'failed' && doc.error_message
  )
  if (failed.length === 0) return null

  return (
    <div style={{ marginTop: 16 }}>
      <h4 style={{ marginBottom: 8 }}>Why these failed</h4>
      <div className="stack" style={{ gap: 8 }}>
        {failed.map((doc) => (
          <Alert key={doc.id} tone="warn">
            {/*
              Extractor messages are scrubbed server-side and are rendered as
              text here regardless -- a filename can contain anything.
            */}
            <strong>{doc.title}</strong>: {doc.error_message}
          </Alert>
        ))}
      </div>
    </div>
  )
}