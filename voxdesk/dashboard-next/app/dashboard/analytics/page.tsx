"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { getToken } from "@/lib/auth";
import { titleCase } from "@/lib/format";
import type { OverviewResponse } from "@/lib/types";

function Section({
  title,
  entries,
}: {
  title: string;
  entries: Record<string, unknown>;
}) {
  return (
    <section className="card">
      <h2>{title}</h2>
      <dl className="kv">
        {Object.entries(entries).map(([key, value]) => (
          <div key={key}>
            <dt>{titleCase(key)}</dt>
            <dd>{String(value)}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

export default function AnalyticsPage() {
  const [overview, setOverview] = useState<OverviewResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) return;
    let cancelled = false;
    api
      .overview()
      .then((result) => {
        if (!cancelled) setOverview(result);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load analytics");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <div className="error-banner">{error}</div>;
  if (!overview) return <div className="loading">Loading…</div>;

  return (
    <>
      <header className="page-head">
        <h1>Analytics</h1>
        <p className="muted">Conversion, operations, integrations and usage.</p>
      </header>

      <Section title="Conversion" entries={overview.conversion} />
      <Section title="Operations" entries={overview.operations} />
      <Section title="Integrations" entries={overview.integrations} />
      {overview.usage ? <Section title="Usage" entries={overview.usage} /> : null}
    </>
  );
}
