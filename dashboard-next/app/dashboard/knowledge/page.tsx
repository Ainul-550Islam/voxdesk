"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { getToken } from "@/lib/auth";
import { formatDateTime, titleCase } from "@/lib/format";
import type {
  DocumentListResponse,
  KnowledgeStats,
  SearchResponse,
} from "@/lib/types";

export default function KnowledgePage() {
  const [documents, setDocuments] = useState<DocumentListResponse | null>(null);
  const [stats, setStats] = useState<KnowledgeStats | null>(null);
  const [search, setSearch] = useState("");
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [searching, setSearching] = useState(false);

  useEffect(() => {
    if (!getToken()) return;
    let cancelled = false;
    Promise.all([api.knowledgeDocuments(), api.knowledgeStats()])
      .then(([docs, stat]) => {
        if (cancelled) return;
        setDocuments(docs);
        setStats(stat);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load knowledge");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function runSearch(event: React.FormEvent) {
    event.preventDefault();
    const query = search.trim();
    if (!query) return;
    setSearching(true);
    setError(null);
    try {
      setResults(await api.knowledgeSearch(query));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Search failed");
    } finally {
      setSearching(false);
    }
  }

  if (error) return <div className="error-banner">{error}</div>;
  if (!documents || !stats) return <div className="loading">Loading…</div>;

  return (
    <>
      <header className="page-head">
        <h1>Knowledge base</h1>
        <p className="muted">
          {stats.total_documents} documents · {stats.searchable_chunks} searchable
          chunks · model {stats.embedding_model}
        </p>
      </header>

      <form className="filters" onSubmit={runSearch}>
        <input
          aria-label="Search knowledge"
          placeholder="Search the knowledge base…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <button type="submit" className="primary" disabled={searching}>
          {searching ? "Searching…" : "Search"}
        </button>
      </form>

      {results ? (
        <section className="card">
          <h2>
            Results for “{results.query}” ({results.count})
          </h2>
          {results.results.length === 0 ? (
            <div className="empty">No matching chunks.</div>
          ) : (
            <ul className="hit-list">
              {results.results.map((hit) => (
                <li key={hit.chunk_id}>
                  <div className="hit-head">
                    <strong>{hit.title}</strong>
                    <span className="muted">score {hit.score.toFixed(3)}</span>
                  </div>
                  <p>{hit.text}</p>
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : null}

      <section className="card">
        <h2>Documents</h2>
        {documents.documents.length === 0 ? (
          <div className="empty">No documents uploaded yet.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Status</th>
                  <th>Type</th>
                  <th>Chunks</th>
                  <th>Size</th>
                  <th>Indexed</th>
                </tr>
              </thead>
              <tbody>
                {documents.documents.map((doc) => (
                  <tr key={doc.id}>
                    <td>{doc.title}</td>
                    <td>
                      <span className={`badge ${doc.status}`}>{titleCase(doc.status)}</span>
                    </td>
                    <td>{doc.source_type}</td>
                    <td>{doc.chunk_count}</td>
                    <td>{doc.file_size}</td>
                    <td>{formatDateTime(doc.indexed_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  );
}
