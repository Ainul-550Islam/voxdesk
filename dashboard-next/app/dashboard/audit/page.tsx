"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { getToken } from "@/lib/auth";
import { formatDateTime, titleCase } from "@/lib/format";
import type { AuditEntry } from "@/lib/types";

export default function AuditPage() {
  const [audit, setAudit] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) return;
    let cancelled = false;
    api
      .teamAudit()
      .then((rows) => {
        if (!cancelled) setAudit(rows);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load audit log");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <div className="error-banner">{error}</div>;
  if (!audit) return <div className="loading">Loading…</div>;

  return (
    <>
      <header className="page-head">
        <h1>Audit log</h1>
        <p className="muted">
          Security events, newest first. Detail never carries credentials.
        </p>
      </header>

      <section className="card">
        {audit.length === 0 ? (
          <div className="empty">No audit events.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When</th>
                  <th>Action</th>
                  <th>Actor</th>
                  <th>IP address</th>
                  <th>Detail</th>
                </tr>
              </thead>
              <tbody>
                {audit.map((entry) => (
                  <tr key={entry.id}>
                    <td>{formatDateTime(entry.created_at)}</td>
                    <td>{titleCase(entry.action)}</td>
                    <td>{entry.actor_email || "—"}</td>
                    <td>{entry.ip_address || "—"}</td>
                    <td>
                      {Object.keys(entry.detail).length > 0
                        ? JSON.stringify(entry.detail)
                        : "—"}
                    </td>
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
