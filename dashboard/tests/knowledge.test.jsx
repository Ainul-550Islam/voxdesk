/**
 * The /knowledge page.
 *
 * The fixtures are the real serialized shapes, captured by driving the actual
 * FastAPI routes with httpx and printing the JSON -- including the naive
 * `created_at` (no trailing `Z`) that SQLite hands back, which is exactly the
 * detail an invented fixture gets wrong and which `lib/format` has to treat
 * as UTC.
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import App from '../src/App'
import {
  EMPTY_OVERVIEW, installFetch, makeMe, OWNER_PERMISSIONS,
  RESTRICTED_PERMISSIONS, sessionRoutes, VIEWER_PERMISSIONS,
} from './harness'

const LIMITS = {
  max_file_mb: 20,
  max_documents: 2000,
  supported_types: ['pdf', 'docx', 'txt', 'md', 'csv', 'json'],
}

const READY_DOC = {
  id: 'doc-ready',
  title: 'Patient Handbook',
  status: 'ready',
  source_type: 'upload',
  original_filename: 'handbook.pdf',
  file_type: 'pdf',
  file_size: 1048576,
  version: 1,
  chunk_count: 42,
  char_count: 18000,
  token_estimate: 4500,
  embedding_model: 'hashing-v1',
  embedding_dimensions: 4096,
  error_message: null,
  metadata: {},
  created_at: '2026-08-14T18:30:00.123456',
  updated_at: '2026-08-14T18:31:00.000000',
  indexed_at: '2026-08-14T18:31:00.000000',
  is_searchable: true,
}

const PROCESSING_DOC = {
  ...READY_DOC,
  id: 'doc-processing',
  title: 'Price List',
  original_filename: 'prices.csv',
  file_type: 'csv',
  status: 'processing',
  chunk_count: 0,
  indexed_at: null,
  is_searchable: false,
}

const FAILED_DOC = {
  ...READY_DOC,
  id: 'doc-failed',
  title: 'Scanned Form',
  original_filename: 'form.pdf',
  status: 'failed',
  chunk_count: 0,
  indexed_at: null,
  is_searchable: false,
  error_message:
    'no text could be extracted -- this PDF may be a scan, which needs OCR',
}

const ARCHIVED_DOC = {
  ...READY_DOC,
  id: 'doc-archived',
  title: 'Old Policy',
  original_filename: 'policy-2019.txt',
  file_type: 'txt',
  status: 'archived',
  is_searchable: false,
}

const STATS = {
  documents: { ready: 1, processing: 1, failed: 1, archived: 1 },
  total_documents: 4,
  total_chunks: 42,
  searchable_chunks: 42,
  embedding_model: 'hashing-v1',
  embedding_dimensions: 4096,
}

const EMPTY_STATS = {
  documents: {},
  total_documents: 0,
  total_chunks: 0,
  searchable_chunks: 0,
  embedding_model: 'hashing-v1',
  embedding_dimensions: 4096,
}

function docList(documents, total = documents.length) {
  return { documents, total, limits: LIMITS }
}

function backend(me = makeMe(), extra = {}) {
  return {
    ...sessionRoutes(me),
    'GET /api/analytics/overview': { body: EMPTY_OVERVIEW },
    'GET /api/knowledge/stats': { body: STATS },
    'GET /api/knowledge/documents': {
      body: docList([READY_DOC, PROCESSING_DOC, FAILED_DOC, ARCHIVED_DOC], 4),
    },
    ...extra,
  }
}

async function openKnowledge(me = makeMe(), extra = {}) {
  window.location.hash = '#/knowledge'
  const fetched = installFetch(backend(me, extra))
  const view = render(<App />)
  await screen.findByRole('heading', { name: /^knowledge$/i, level: 1 })
  return { ...fetched, ...view }
}

describe('route rendering', () => {
  it('renders the knowledge page at /knowledge', async () => {
    await openKnowledge()
    expect(await screen.findByRole('region', { name: /^documents$/i }))
      .toBeInTheDocument()
  })

  it('is reachable from the sidebar for any role with knowledge:read', async () => {
    await openKnowledge(makeMe(VIEWER_PERMISSIONS))
    const nav = screen.getByRole('navigation', { name: /main navigation/i })
    expect(within(nav).getByRole('link', { name: /knowledge/i }))
      .toBeInTheDocument()
  })

  it('never sends a tenant id -- the server derives it from the token', async () => {
    const { calls } = await openKnowledge()
    await screen.findByText('Patient Handbook')

    const knowledgeCalls = calls.filter((c) => c.path.includes('/knowledge'))
    expect(knowledgeCalls.length).toBeGreaterThan(0)
    for (const call of knowledgeCalls) {
      expect(call.path).not.toMatch(/tenants?[/=]/)
      expect(call.path).not.toContain('tenant_id')
    }
  })
})

describe('real API response rendering', () => {
  it('renders document metadata from the response', async () => {
    await openKnowledge()

    // Scoped to the row: several fixtures share a size and a chunk count.
    const row = (await screen.findByText('Patient Handbook')).closest('tr')
    expect(within(row).getByText('handbook.pdf')).toBeInTheDocument()
    expect(within(row).getByText('pdf')).toBeInTheDocument()
    expect(within(row).getByText('42')).toBeInTheDocument()   // chunk count
    expect(within(row).getByText('1.0 MB')).toBeInTheDocument()
  })

  it('renders the real statistics and no invented ones', async () => {
    await openKnowledge()

    const ready = await screen.findByText('Ready')
    expect(ready.closest('.card')).toHaveTextContent('1')
    expect(screen.getByText(/searchable chunks/i).closest('.card'))
      .toHaveTextContent('42')
    expect(screen.getByText(/hashing-v1/)).toBeInTheDocument()
    expect(screen.getByText(/4,096 dimensions/)).toBeInTheDocument()
  })

  it('formats timestamps in the tenant timezone, not the browser one', async () => {
    await openKnowledge()
    // 18:30 UTC is 14:30 in America/New_York, the fixture tenant's zone. The
    // backend value is naive and must be read as UTC, not as local time.
    const row = (await screen.findByText('Patient Handbook')).closest('tr')
    expect(within(row).getByText(/14:30/)).toBeInTheDocument()
    expect(within(row).queryByText(/18:30/)).toBeNull()
  })
})

describe('ingestion lifecycle', () => {
  it('shows each backend status verbatim', async () => {
    await openKnowledge()
    const table = await screen.findByRole('table')

    for (const status of ['ready', 'processing', 'failed', 'archived']) {
      expect(within(table).getByText(status)).toBeInTheDocument()
    }
  })

  it('marks only a ready document as searchable', async () => {
    await openKnowledge()
    const table = await screen.findByRole('table')

    const searchable = within(table).getAllByText('Searchable')
    expect(searchable).toHaveLength(1)
    expect(searchable[0].closest('tr')).toHaveTextContent('Patient Handbook')
  })

  it('explains why a document failed', async () => {
    await openKnowledge()
    expect(await screen.findByText(/which needs OCR/i)).toBeInTheDocument()
  })

  it('says it is auto-refreshing while work is in flight', async () => {
    await openKnowledge()
    expect(await screen.findByText(/still being processed/i)).toBeInTheDocument()
  })

  it('does not poll when nothing is in flight', async () => {
    vi.useFakeTimers()
    try {
      window.location.hash = '#/knowledge'
      const { calls } = installFetch(
        backend(makeMe(), {
          'GET /api/knowledge/documents': { body: docList([READY_DOC], 1) },
        })
      )
      render(<App />)
      await vi.waitFor(() =>
        expect(calls.some((c) => c.path.includes('/knowledge/documents')))
          .toBe(true)
      )

      const before = calls.filter((c) => c.path.includes('/knowledge')).length
      await vi.advanceTimersByTimeAsync(15000)
      const after = calls.filter((c) => c.path.includes('/knowledge')).length

      expect(after).toBe(before)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('permission gating', () => {
  it('hides every mutating control from a read-only account', async () => {
    await openKnowledge(makeMe(VIEWER_PERMISSIONS))
    await screen.findByText('Patient Handbook')

    expect(screen.queryByRole('button', { name: /upload/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^retry$/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /reindex/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /archive/i })).toBeNull()
    expect(screen.queryByRole('button', { name: /^delete/i })).toBeNull()
    expect(screen.queryByLabelText(/^file$/i)).toBeNull()
  })

  it('still shows a read-only account the documents and stats', async () => {
    await openKnowledge(makeMe(VIEWER_PERMISSIONS))
    expect(await screen.findByText('Patient Handbook')).toBeInTheDocument()
    expect(screen.getByText(/searchable chunks/i)).toBeInTheDocument()
  })

  it('gives a writer upload, retry and archive but not permanent delete', async () => {
    // A manager has knowledge:write but not knowledge:delete.
    const manager = [...VIEWER_PERMISSIONS, 'knowledge:write']
    await openKnowledge(makeMe(manager))
    await screen.findByText('Patient Handbook')

    expect(screen.getByRole('button', { name: /^upload$/i })).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /archive/i }).length)
      .toBeGreaterThan(0)
    // Destroying evidence needs knowledge:delete.
    expect(screen.queryByRole('button', { name: /^delete$/i })).toBeNull()
  })

  it('gives an owner the permanent delete control', async () => {
    await openKnowledge(makeMe(OWNER_PERMISSIONS))
    await screen.findByText('Patient Handbook')
    expect(screen.getAllByRole('button', { name: /^delete$/i }).length)
      .toBeGreaterThan(0)
  })

  it('refuses the page entirely without knowledge:read', async () => {
    window.location.hash = '#/knowledge'
    installFetch(backend(makeMe(RESTRICTED_PERMISSIONS)))
    render(<App />)

    expect(await screen.findByText(/you do not have access to this page/i))
      .toBeInTheDocument()
  })
})

describe('row actions match backend semantics', () => {
  it('offers Retry for a failed document and Reindex for a ready one', async () => {
    await openKnowledge()
    const table = await screen.findByRole('table')

    const failedRow = within(table).getByText('Scanned Form').closest('tr')
    expect(within(failedRow).getByRole('button', { name: /retry/i }))
      .toBeInTheDocument()

    const readyRow = within(table).getByText('Patient Handbook').closest('tr')
    expect(within(readyRow).getByRole('button', { name: /reindex/i }))
      .toBeInTheDocument()
  })

  it('offers no reindex while a document is processing', async () => {
    // The backend treats that as a no-op, so the button would lie.
    await openKnowledge()
    const table = await screen.findByRole('table')
    const row = within(table).getByText('Price List').closest('tr')

    expect(within(row).queryByRole('button', { name: /reindex|retry/i }))
      .toBeNull()
  })

  it('offers Restore instead of Archive for an archived document', async () => {
    // The backend 409s on reindexing an archived document.
    await openKnowledge()
    const table = await screen.findByRole('table')
    const row = within(table).getByText('Old Policy').closest('tr')

    expect(within(row).getByRole('button', { name: /restore/i }))
      .toBeInTheDocument()
    expect(within(row).queryByRole('button', { name: /^archive$/i })).toBeNull()
    expect(within(row).queryByRole('button', { name: /reindex/i })).toBeNull()
  })

  it('reindexes through the real endpoint and refreshes', async () => {
    const user = userEvent.setup()
    const { calls } = await openKnowledge(makeMe(), {
      'POST /api/knowledge/documents/doc-failed/reindex': { body: FAILED_DOC },
    })

    const table = await screen.findByRole('table')
    const row = within(table).getByText('Scanned Form').closest('tr')
    await user.click(within(row).getByRole('button', { name: /retry/i }))

    expect(await screen.findByText(/reindexing started/i)).toBeInTheDocument()
    expect(calls.some((c) =>
      c.method === 'POST'
      && c.path === '/api/knowledge/documents/doc-failed/reindex')).toBe(true)
  })

  it('restores through the real endpoint', async () => {
    const user = userEvent.setup()
    const { calls } = await openKnowledge(makeMe(), {
      'POST /api/knowledge/documents/doc-archived/restore': { body: READY_DOC },
    })

    const table = await screen.findByRole('table')
    const row = within(table).getByText('Old Policy').closest('tr')
    await user.click(within(row).getByRole('button', { name: /restore/i }))

    expect(await screen.findByText(/document restored/i)).toBeInTheDocument()
    expect(calls.some((c) =>
      c.path === '/api/knowledge/documents/doc-archived/restore')).toBe(true)
  })

  it('surfaces a failed action without breaking the page', async () => {
    const user = userEvent.setup()
    await openKnowledge(makeMe(), {
      'POST /api/knowledge/documents/doc-failed/reindex': {
        status: 409, body: { detail: 'This document is archived.' },
      },
    })

    const table = await screen.findByRole('table')
    const row = within(table).getByText('Scanned Form').closest('tr')
    await user.click(within(row).getByRole('button', { name: /retry/i }))

    expect(await screen.findByText(/this document is archived/i))
      .toBeInTheDocument()
    expect(screen.getByText('Patient Handbook')).toBeInTheDocument()
  })
})

describe('archive and delete confirmation', () => {
  it('asks before archiving and sends no request until confirmed', async () => {
    const user = userEvent.setup()
    const { calls } = await openKnowledge(makeMe(), {
      'DELETE /api/knowledge/documents/doc-ready': {
        body: { ok: true, status: 'archived' },
      },
    })

    const table = await screen.findByRole('table')
    const row = within(table).getByText('Patient Handbook').closest('tr')
    await user.click(within(row).getByRole('button', { name: /^archive$/i }))

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(/archive this document/i)
    // Nothing sent yet.
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)

    await user.click(within(dialog).getByRole('button', { name: /^archive$/i }))

    expect(await screen.findByText(/document archived/i)).toBeInTheDocument()
    const del = calls.find((c) => c.method === 'DELETE')
    expect(del.path).toBe('/api/knowledge/documents/doc-ready?hard=false')
  })

  it('cancelling sends nothing', async () => {
    const user = userEvent.setup()
    const { calls } = await openKnowledge()

    const table = await screen.findByRole('table')
    const row = within(table).getByText('Patient Handbook').closest('tr')
    await user.click(within(row).getByRole('button', { name: /^archive$/i }))

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: /cancel/i }))

    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(calls.some((c) => c.method === 'DELETE')).toBe(false)
  })

  it('warns that a permanent delete cannot be undone and sends hard=true', async () => {
    const user = userEvent.setup()
    const { calls } = await openKnowledge(makeMe(), {
      'DELETE /api/knowledge/documents/doc-ready': {
        body: { ok: true, status: 'deleted' },
      },
    })

    const table = await screen.findByRole('table')
    const row = within(table).getByText('Patient Handbook').closest('tr')
    await user.click(within(row).getByRole('button', { name: /^delete$/i }))

    const dialog = await screen.findByRole('dialog')
    expect(dialog).toHaveTextContent(/cannot be undone/i)
    await user.click(
      within(dialog).getByRole('button', { name: /delete permanently/i })
    )

    expect(await screen.findByText(/permanently deleted/i)).toBeInTheDocument()
    const del = calls.find((c) => c.method === 'DELETE')
    expect(del.path).toBe('/api/knowledge/documents/doc-ready?hard=true')
  })
})

describe('upload', () => {
  const file = (name, type = 'text/plain', size = 10) => {
    const blob = new File(['x'.repeat(size)], name, { type })
    return blob
  }

  it('uploads through the real multipart endpoint', async () => {
    const user = userEvent.setup()
    const { calls } = await openKnowledge(makeMe(), {
      'POST /api/knowledge/documents': {
        status: 202,
        body: { ...READY_DOC, id: 'new-doc', status: 'uploaded', chunk_count: 0 },
      },
    })

    await user.upload(screen.getByLabelText(/^file$/i), file('notes.txt'))
    await user.click(screen.getByRole('button', { name: /^upload$/i }))

    expect(await screen.findByText(/upload accepted/i)).toBeInTheDocument()

    const post = calls.find(
      (c) => c.method === 'POST' && c.path === '/api/knowledge/documents'
    )
    expect(post).toBeTruthy()
    // Multipart: the browser must set its own boundary, so no JSON header.
    expect(post.options.body).toBeInstanceOf(FormData)
    expect(post.options.headers['Content-Type']).toBeUndefined()
  })

  it('rejects an unsupported type client-side without a round trip', async () => {
    // `accept` only filters the picker dialogue; a drag-and-drop or a
    // scripted drop bypasses it entirely, which is why the page validates
    // rather than trusting the attribute. `applyAccept: false` reproduces
    // that bypass -- otherwise userEvent enforces `accept` for us and the
    // check under test never runs.
    const user = userEvent.setup({ applyAccept: false })
    const { calls } = await openKnowledge()

    await user.upload(
      screen.getByLabelText(/^file$/i),
      file('payload.exe', 'application/octet-stream')
    )

    expect(await screen.findByText(/file type is not supported/i))
      .toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^upload$/i })).toBeDisabled()
    // Nothing was sent to the documents endpoint. (The session's
    // POST /auth/refresh is unrelated, so match on the path too.)
    expect(calls.some((c) =>
      c.method === 'POST' && c.path === '/api/knowledge/documents')).toBe(false)
  })

  it('rejects an oversized file against the limit the server reported', async () => {
    const user = userEvent.setup()
    await openKnowledge()

    const big = new File(['x'], 'huge.pdf', { type: 'application/pdf' })
    Object.defineProperty(big, 'size', { value: 21 * 1024 * 1024 })
    await user.upload(screen.getByLabelText(/^file$/i), big)

    expect(await screen.findByText(/larger than the 20 MB limit/i))
      .toBeInTheDocument()
  })

  it('surfaces a server rejection, because the backend is authoritative', async () => {
    const user = userEvent.setup()
    // Client-side checks pass; the server sniffs magic bytes and refuses.
    await openKnowledge(makeMe(), {
      'POST /api/knowledge/documents': {
        status: 400,
        body: { detail: 'this file is not a valid pdf' },
      },
    })

    await user.upload(
      screen.getByLabelText(/^file$/i),
      file('fake.pdf', 'application/pdf')
    )
    await user.click(screen.getByRole('button', { name: /^upload$/i }))

    expect(await screen.findByText(/not a valid pdf/i)).toBeInTheDocument()
  })

  it('reports a duplicate as already present rather than a new upload', async () => {
    const user = userEvent.setup()
    await openKnowledge(makeMe(), {
      // The backend answers 200 with the existing row, not 202.
      'POST /api/knowledge/documents': { status: 200, body: READY_DOC },
    })

    await user.upload(screen.getByLabelText(/^file$/i), file('handbook.pdf'))
    await user.click(screen.getByRole('button', { name: /^upload$/i }))

    expect(await screen.findByText(/already in the knowledge base/i))
      .toBeInTheDocument()
  })

  it('advertises only the file types the server said it supports', async () => {
    await openKnowledge()
    const input = screen.getByLabelText(/^file$/i)
    expect(input).toHaveAttribute('accept', '.pdf,.docx,.txt,.md,.csv,.json')
  })
})

describe('filtering and pagination use the server', () => {
  it('sends the status filter to the backend', async () => {
    const user = userEvent.setup()
    const { calls } = await openKnowledge()
    await screen.findByText('Patient Handbook')

    await user.selectOptions(screen.getByLabelText(/filter by status/i), 'failed')

    await waitFor(() =>
      expect(calls.some((c) => c.path.includes('status=failed'))).toBe(true)
    )
  })

  it('paginates on the server with limit and offset', async () => {
    const user = userEvent.setup()
    const many = Array.from({ length: 25 }, (_, index) => ({
      ...READY_DOC, id: `doc-${index}`, title: `Document ${index}`,
    }))
    const { calls } = await openKnowledge(makeMe(), {
      'GET /api/knowledge/documents': { body: docList(many, 60) },
    })

    await screen.findByText('Document 0')
    expect(screen.getByText(/1–25 of 60 documents/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /next/i }))

    await waitFor(() =>
      expect(calls.some((c) => c.path.includes('offset=25'))).toBe(true)
    )
  })
})

describe('empty and error states', () => {
  it('shows an empty state when the tenant has no documents', async () => {
    await openKnowledge(makeMe(), {
      'GET /api/knowledge/stats': { body: EMPTY_STATS },
      'GET /api/knowledge/documents': { body: docList([], 0) },
    })

    expect(await screen.findByText(/no documents yet/i)).toBeInTheDocument()
    // Real zeroes from the API, not invented ones.
    expect(screen.getByText(/searchable chunks/i).closest('.card'))
      .toHaveTextContent('0')
  })

  it('distinguishes an empty filter result from an empty knowledge base', async () => {
    const user = userEvent.setup()
    await openKnowledge(makeMe(), {
      'GET /api/knowledge/documents': ({ path }) =>
        path.includes('status=archived')
          ? { body: docList([], 0) }
          : { body: docList([READY_DOC], 1) },
    })
    await screen.findByText('Patient Handbook')

    await user.selectOptions(
      screen.getByLabelText(/filter by status/i), 'archived'
    )

    expect(await screen.findByText(/no archived documents/i)).toBeInTheDocument()
  })

  it('offers a retry when the list fails', async () => {
    await openKnowledge(makeMe(), {
      'GET /api/knowledge/documents': { status: 500, body: { detail: 'boom' } },
    })

    expect(await screen.findByText(/could not load documents/i))
      .toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: /try again/i }).length)
      .toBeGreaterThan(0)
  })

  it('keeps the document list usable when only the stats fail', async () => {
    await openKnowledge(makeMe(), {
      'GET /api/knowledge/stats': { status: 500, body: { detail: 'boom' } },
    })

    expect(await screen.findByText(/could not load the knowledge statistics/i))
      .toBeInTheDocument()
    expect(screen.getByText('Patient Handbook')).toBeInTheDocument()
  })
})

describe('security', () => {
  it('renders a hostile filename and title as text, not markup', async () => {
    const hostile = '<img src=x onerror="window.__kbPwned=1">'
    const { container } = await openKnowledge(makeMe(), {
      'GET /api/knowledge/documents': {
        body: docList([{
          ...FAILED_DOC,
          title: hostile,
          original_filename: `${hostile}.pdf`,
          error_message: `could not parse ${hostile}`,
        }], 1),
      },
    })
    await screen.findByRole('table')

    expect(container.querySelector('img')).toBeNull()
    expect(window.__kbPwned).toBeUndefined()
    expect(screen.getAllByText(/onerror/).length).toBeGreaterThan(0)
  })

  it('never renders a storage path or credential the API might add', async () => {
    await openKnowledge(makeMe(), {
      'GET /api/knowledge/documents': {
        body: docList([{
          ...READY_DOC,
          // None of these are in DocumentOut; defence in depth if one ever is.
          source_uri: 's3://voxdesk-private/tenant/secret-key.pdf',
          storage_path: '/var/knowledge/tenant/abc.pdf',
          embedding_api_key: 'sk-live-NEVER-RENDER',
        }], 1),
      },
    })
    await screen.findByText('Patient Handbook')

    const text = document.body.textContent
    expect(text).not.toContain('s3://')
    expect(text).not.toContain('/var/knowledge')
    expect(text).not.toContain('sk-live-NEVER-RENDER')
  })

  it('puts nothing in localStorage', async () => {
    await openKnowledge()
    await screen.findByText('Patient Handbook')

    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })
})
