"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { getToken } from "@/lib/auth";
import CallsTable from "@/components/calls-table";
import type { CallListResponse } from "@/lib/types";

const LIMIT = 25;

export default function CallsPage() {
  const [tenantId, setTenantId] = useState<string | null>(null);
  const [data, setData] = useState<CallListResponse | null>(null);
  const [status, setStatus] = useState("");
  const [direction, setDirection] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);

  // Resolve the tenant id from /auth/me — it is never taken from user input.
  useEffect(() => {
    if (!getToken()) return;
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (!cancelled) setTenantId(me.user.tenant_id);
      })
      .catch(() => {
        // The dashboard shell's guard handles the redirect; a failed /me here
        // simply leaves the table empty until the token is restored.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const load = useCallback(async () => {
    if (!tenantId) return;
    try {
      setError(null);
      const result = await api.listCalls(tenantId, {
        limit: LIMIT,
        offset,
        status: status || undefined,
        direction: direction || undefined,
        search: search || undefined,
      });
      setData(result);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load calls");
    }
  }, [tenantId, offset, status, direction, search]);

  useEffect(() => {
    load();
  }, [load]);

  const total = data?.total ?? 0;
  const hasPrev = offset > 0;
  const hasNext = data !== null && offset + data.calls.length < total;

  return (
    <>
      <header className="page-head">
        <h1>Calls</h1>
        <p className="muted">Every call is scoped to your tenant on the server.</p>
      </header>

      <div className="filters">
        <select
          aria-label="Status filter"
          value={status}
          onChange={(e) => {
            setStatus(e.target.value);
            setOffset(0);
          }}
        >
          <option value="">All statuses</option>
          <option value="ringing">Ringing</option>
          <option value="in_progress">In progress</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
          <option value="no_answer">No answer</option>
          <option value="transferred">Transferred</option>
        </select>

        <select
          aria-label="Direction filter"
          value={direction}
          onChange={(e) => {
            setDirection(e.target.value);
            setOffset(0);
          }}
        >
          <option value="">All directions</option>
          <option value="inbound">Inbound</option>
          <option value="outbound">Outbound</option>
        </select>

        <input
          aria-label="Search by phone"
          placeholder="Search by phone…"
          value={search}
          onChange={(e) => {
            setSearch(e.target.value);
            setOffset(0);
          }}
        />
      </div>

      {error ? <div className="error-banner">{error}</div> : null}

      {data ? (
        <div className="card">
          <CallsTable calls={data.calls} />
          <div className="pager">
            <button
              type="button"
              disabled={!hasPrev}
              onClick={() => setOffset(Math.max(0, offset - LIMIT))}
            >
              Previous
            </button>
            <span className="muted">
              {total === 0 ? 0 : offset + 1}–{Math.min(offset + LIMIT, total)} of {total}
            </span>
            <button
              type="button"
              disabled={!hasNext}
              onClick={() => setOffset(offset + LIMIT)}
            >
              Next
            </button>
          </div>
        </div>
      ) : (
        <div className="loading">Loading…</div>
      )}
    </>
  );
}
