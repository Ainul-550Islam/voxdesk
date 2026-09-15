"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { useTenantId } from "@/lib/hooks";
import { formatDateTime, formatPhone, titleCase } from "@/lib/format";
import type { Lead } from "@/lib/types";

export default function LeadsPage() {
  const tenantId = useTenantId();
  const [leads, setLeads] = useState<Lead[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!tenantId) return;
    let cancelled = false;
    api
      .leads(tenantId)
      .then((rows) => {
        if (!cancelled) setLeads(rows);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load leads");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [tenantId]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!leads) return <div className="loading">Loading…</div>;

  return (
    <>
      <header className="page-head">
        <h1>Leads</h1>
        <p className="muted">Contact records feeding outbound campaigns.</p>
      </header>

      <section className="card">
        {leads.length === 0 ? (
          <div className="empty">No leads yet.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Phone</th>
                  <th>Email</th>
                  <th>Company</th>
                  <th>Status</th>
                  <th>Score</th>
                  <th>Attempts</th>
                  <th>Next attempt</th>
                </tr>
              </thead>
              <tbody>
                {leads.map((lead) => (
                  <tr key={lead.id}>
                    <td>{lead.name || "—"}</td>
                    <td>{formatPhone(lead.phone)}</td>
                    <td>{lead.email ?? "—"}</td>
                    <td>{lead.company ?? "—"}</td>
                    <td>
                      <span className={`badge ${lead.status}`}>{titleCase(lead.status)}</span>
                    </td>
                    <td>{lead.score ?? "—"}</td>
                    <td>{lead.attempts}</td>
                    <td>{formatDateTime(lead.next_attempt_at)}</td>
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
