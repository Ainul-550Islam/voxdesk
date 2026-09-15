"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { getToken } from "@/lib/auth";
import { formatCurrencyCents, formatDateTime, titleCase } from "@/lib/format";
import type { BillingStatus, Invoice, Plan, UsageResponse } from "@/lib/types";

export default function BillingPage() {
  const [status, setStatus] = useState<BillingStatus | null>(null);
  const [plans, setPlans] = useState<Plan[] | null>(null);
  const [usage, setUsage] = useState<UsageResponse | null>(null);
  const [invoices, setInvoices] = useState<Invoice[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) return;
    let cancelled = false;
    Promise.all([
      api.billingStatus(),
      api.billingPlans(),
      api.billingUsage(),
      api.billingInvoices(),
    ])
      .then(([statusRes, plansRes, usageRes, invoicesRes]) => {
        if (cancelled) return;
        setStatus(statusRes);
        setPlans(plansRes);
        setUsage(usageRes);
        setInvoices(invoicesRes);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load billing");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) return <div className="error-banner">{error}</div>;
  if (!status || !plans || !usage || !invoices) return <div className="loading">Loading…</div>;

  return (
    <>
      <header className="page-head">
        <h1>Billing</h1>
        <p className="muted">
          {status.plan_name} · <span className={`badge ${status.subscription_status}`}>
            {titleCase(status.subscription_status)}
          </span>
        </p>
      </header>

      <section className="card">
        <h2>Subscription</h2>
        <dl className="kv">
          <div>
            <dt>Plan</dt>
            <dd>{status.plan_code}</dd>
          </div>
          <div>
            <dt>Interval</dt>
            <dd>{status.interval}</dd>
          </div>
          <div>
            <dt>Period end</dt>
            <dd>{formatDateTime(status.current_period_end)}</dd>
          </div>
          <div>
            <dt>Cancel at period end</dt>
            <dd>{status.cancel_at_period_end ? "Yes" : "No"}</dd>
          </div>
          <div>
            <dt>Estimated overage</dt>
            <dd>{formatCurrencyCents(status.estimated_overage_cents)}</dd>
          </div>
        </dl>
      </section>

      <section className="card">
        <h2>Usage ({usage.billing_period})</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Metric</th>
                <th>Included</th>
                <th>Used</th>
                <th>Remaining</th>
                <th>Used %</th>
              </tr>
            </thead>
            <tbody>
              {usage.metrics.map((metric) => (
                <tr key={metric.metric}>
                  <td>{titleCase(metric.metric)}</td>
                  <td>{metric.included}</td>
                  <td>{metric.used}</td>
                  <td>{metric.remaining}</td>
                  <td>{Math.round(metric.percent_used)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <h2>Plans</h2>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Plan</th>
                <th>Monthly</th>
                <th>Voice minutes</th>
                <th>SMS segments</th>
                <th>Overage</th>
              </tr>
            </thead>
            <tbody>
              {plans.map((plan) => (
                <tr key={plan.code}>
                  <td>{plan.name}</td>
                  <td>{formatCurrencyCents(plan.monthly_price_cents)}</td>
                  <td>{plan.included_voice_minutes}</td>
                  <td>{plan.included_sms_segments}</td>
                  <td>{plan.overage_enabled ? "Enabled" : "Blocked"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="card">
        <h2>Invoices</h2>
        {invoices.length === 0 ? (
          <div className="empty">No invoices yet.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Period</th>
                  <th>Due</th>
                  <th>Paid</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {invoices.map((invoice) => (
                  <tr key={invoice.id}>
                    <td>
                      <span className={`badge ${invoice.status}`}>
                        {titleCase(invoice.status)}
                      </span>
                    </td>
                    <td>
                      {formatDateTime(invoice.period_start)} – {formatDateTime(invoice.period_end)}
                    </td>
                    <td>{formatCurrencyCents(invoice.amount_due_cents)}</td>
                    <td>{formatCurrencyCents(invoice.amount_paid_cents)}</td>
                    <td>
                      {invoice.hosted_invoice_url ? (
                        <a href={invoice.hosted_invoice_url} target="_blank" rel="noreferrer">
                          View
                        </a>
                      ) : null}
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
