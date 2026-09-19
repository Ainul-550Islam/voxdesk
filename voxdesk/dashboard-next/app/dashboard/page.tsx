"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { getToken } from "@/lib/auth";
import StatCards from "@/components/stat-cards";
import { formatDateTime } from "@/lib/format";
import type { MeResponse, OverviewResponse } from "@/lib/types";

export default function OverviewPage() {
  const [me, setMe] = useState<MeResponse | null>(null);
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) return;
    let cancelled = false;
    (async () => {
      try {
        const [meRes, ovRes] = await Promise.all([api.me(), api.overview()]);
        if (cancelled) return;
        setMe(meRes);
        setOverview(ovRes);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load overview");
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <div className="error-banner">{error}</div>;
  if (!me || !overview) return <div className="loading">Loading…</div>;

  const tenantName =
    typeof me.tenant?.name === "string" ? me.tenant.name : "Overview";

  return (
    <>
      <header className="page-head">
        <h1>{tenantName}</h1>
        {overview.window ? (
          <p className="muted">
            {formatDateTime(overview.window.start)} – {formatDateTime(overview.window.end)}
          </p>
        ) : null}
      </header>

      <StatCards stats={overview.calls} title="Calls" />

      <section className="card">
        <h2>Conversion</h2>
        <dl className="kv">
          {Object.entries(overview.conversion).map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{String(value)}</dd>
            </div>
          ))}
        </dl>
      </section>

      <section className="card">
        <h2>Operations</h2>
        <dl className="kv">
          {Object.entries(overview.operations).map(([key, value]) => (
            <div key={key}>
              <dt>{key}</dt>
              <dd>{String(value)}</dd>
            </div>
          ))}
        </dl>
      </section>
    </>
  );
}
