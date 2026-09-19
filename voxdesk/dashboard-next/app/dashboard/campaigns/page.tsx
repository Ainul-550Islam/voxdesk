"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { getToken } from "@/lib/auth";
import { formatDays, titleCase } from "@/lib/format";
import type { Campaign, CampaignResults } from "@/lib/types";

function daysLabel(daysOfWeek: number[]): string {
  return formatDays(daysOfWeek);
}

export default function CampaignsPage() {
  const [campaigns, setCampaigns] = useState<Campaign[] | null>(null);
  const [results, setResults] = useState<Record<string, CampaignResults>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) return;
    let cancelled = false;
    api
      .campaigns()
      .then(async (rows) => {
        if (cancelled) return;
        setCampaigns(rows);
        const map: Record<string, CampaignResults> = {};
        await Promise.all(
          rows.map(async (campaign) => {
            try {
              map[campaign.id] = await api.campaignResults(campaign.id);
            } catch {
              map[campaign.id] = { metrics: {}, eligibility: {} };
            }
          }),
        );
        if (!cancelled) setResults(map);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load campaigns");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <div className="error-banner">{error}</div>;
  if (!campaigns) return <div className="loading">Loading…</div>;

  return (
    <>
      <header className="page-head">
        <h1>Campaigns</h1>
        <p className="muted">Outbound call runs with their latest results.</p>
      </header>

      {campaigns.length === 0 ? (
        <div className="card empty">No campaigns defined.</div>
      ) : (
        campaigns.map((campaign) => {
          const result = results[campaign.id];
          return (
            <section className="card" key={campaign.id}>
              <h2>
                {campaign.name}{" "}
                <span className={`badge ${campaign.state}`}>{titleCase(campaign.state)}</span>
              </h2>
              <dl className="kv">
                <div>
                  <dt>Goal</dt>
                  <dd>{campaign.goal}</dd>
                </div>
                <div>
                  <dt>Channel</dt>
                  <dd>{campaign.channel}</dd>
                </div>
                <div>
                  <dt>Days</dt>
                  <dd>{daysLabel(campaign.days_of_week)}</dd>
                </div>
                <div>
                  <dt>Window</dt>
                  <dd>
                    {campaign.daily_start} – {campaign.daily_end}
                  </dd>
                </div>
                <div>
                  <dt>Rate</dt>
                  <dd>{campaign.calls_per_minute}/min</dd>
                </div>
                <div>
                  <dt>Daily limit</dt>
                  <dd>{campaign.daily_limit}</dd>
                </div>
              </dl>
              {result ? (
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        {Object.keys(result.metrics).map((key) => (
                          <th key={key}>{titleCase(key)}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      <tr>
                        {Object.values(result.metrics).map((value, index) => (
                          <td key={index}>{value}</td>
                        ))}
                      </tr>
                    </tbody>
                  </table>
                </div>
              ) : null}
            </section>
          );
        })
      )}
    </>
  );
}
