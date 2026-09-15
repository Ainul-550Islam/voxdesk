"use client";

import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { getToken } from "@/lib/auth";
import { formatDateTime, titleCase } from "@/lib/format";
import type {
  IntegrationListResponse,
  ProviderCatalogueResponse,
  SyncListResponse,
} from "@/lib/types";

export default function IntegrationsPage() {
  const [integrations, setIntegrations] = useState<IntegrationListResponse | null>(null);
  const [providers, setProviders] = useState<ProviderCatalogueResponse | null>(null);
  const [syncs, setSyncs] = useState<SyncListResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!getToken()) return;
    try {
      setError(null);
      const [ints, prov, syn] = await Promise.all([
        api.crmIntegrations(),
        api.crmProviders(),
        api.crmSyncs(),
      ]);
      setIntegrations(ints);
      setProviders(prov);
      setSyncs(syn);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Failed to load integrations");
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function disconnect(provider: string) {
    setBusy(provider);
    setError(null);
    try {
      await api.crmDisconnect(provider);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Disconnect failed");
    } finally {
      setBusy(null);
    }
  }

  if (error) return <div className="error-banner">{error}</div>;
  if (!integrations || !providers || !syncs) return <div className="loading">Loading…</div>;

  const configured = new Set(integrations.integrations.map((i) => i.provider));

  return (
    <>
      <header className="page-head">
        <h1>Integrations</h1>
        <p className="muted">CRM providers. Credentials never leave the server.</p>
      </header>

      <section className="card">
        <h2>Connected</h2>
        {integrations.integrations.length === 0 ? (
          <div className="empty">No CRM integrations connected.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Provider</th>
                  <th>Connected</th>
                  <th>Health</th>
                  <th>Last checked</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {integrations.integrations.map((integration) => (
                  <tr key={integration.provider}>
                    <td>{integration.provider}</td>
                    <td>
                      <span className={`badge ${integration.connected ? "completed" : "failed"}`}>
                        {integration.connected ? "Yes" : "No"}
                      </span>
                    </td>
                    <td>
                      {integration.last_health_ok === null
                        ? "—"
                        : integration.last_health_ok
                          ? "Healthy"
                          : "Unhealthy"}
                    </td>
                    <td>{formatDateTime(integration.last_health_check_at)}</td>
                    <td>
                      <button
                        type="button"
                        className="danger"
                        disabled={busy === integration.provider}
                        onClick={() => disconnect(integration.provider)}
                      >
                        {busy === integration.provider ? "Disconnecting…" : "Disconnect"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <h2>Available providers</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Provider</th>
                <th>Capabilities</th>
                <th>State</th>
              </tr>
            </thead>
            <tbody>
              {providers.providers.map((provider) => (
                <tr key={provider.provider}>
                  <td>{provider.provider}</td>
                  <td>{provider.capabilities.join(", ")}</td>
                  <td>{configured.has(provider.provider) ? "Configured" : "Not configured"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <h2>Recent syncs</h2>
        {syncs.syncs.length === 0 ? (
          <div className="empty">No CRM sync attempts recorded.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Provider</th>
                  <th>Entity</th>
                  <th>Status</th>
                  <th>Attempts</th>
                  <th>Last attempt</th>
                </tr>
              </thead>
              <tbody>
                {syncs.syncs.map((sync, index) => (
                  <tr key={index}>
                    <td>{sync.provider}</td>
                    <td>{sync.entity_type}</td>
                    <td>
                      <span className={`badge ${sync.status}`}>{titleCase(sync.status)}</span>
                    </td>
                    <td>{sync.attempt_count}</td>
                    <td>{formatDateTime(sync.last_attempt_at)}</td>
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
