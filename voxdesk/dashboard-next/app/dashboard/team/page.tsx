"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { getToken } from "@/lib/auth";
import { formatDateTime, titleCase } from "@/lib/format";
import type { AuditEntry, User } from "@/lib/types";

export default function TeamPage() {
  const [users, setUsers] = useState<User[] | null>(null);
  const [audit, setAudit] = useState<AuditEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) return;
    let cancelled = false;
    Promise.all([api.teamUsers(), api.teamAudit()])
      .then(([userRows, auditRows]) => {
        if (cancelled) return;
        setUsers(userRows);
        setAudit(auditRows);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load team");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <div className="error-banner">{error}</div>;
  if (!users || !audit) return <div className="loading">Loading…</div>;

  return (
    <>
      <header className="page-head">
        <h1>Team</h1>
        <p className="muted">Operators and the security events they generated.</p>
      </header>

      <section className="card">
        <h2>Users ({users.length})</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Name</th>
                <th>Email</th>
                <th>Role</th>
                <th>Active</th>
                <th>Last login</th>
              </tr>
            </thead>
            <tbody>
              {users.map((user) => (
                <tr key={user.id}>
                  <td>{user.full_name || "—"}</td>
                  <td>{user.email}</td>
                  <td>{titleCase(user.role)}</td>
                  <td>{user.is_active ? "Yes" : "No"}</td>
                  <td>{formatDateTime(user.last_login_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <h2>Audit log</h2>
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
                  <th>IP</th>
                </tr>
              </thead>
              <tbody>
                {audit.map((entry) => (
                  <tr key={entry.id}>
                    <td>{formatDateTime(entry.created_at)}</td>
                    <td>{titleCase(entry.action)}</td>
                    <td>{entry.actor_email || "—"}</td>
                    <td>{entry.ip_address || "—"}</td>
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
