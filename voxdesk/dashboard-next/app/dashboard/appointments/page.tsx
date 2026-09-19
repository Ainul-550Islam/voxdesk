"use client";

import { useEffect, useState } from "react";
import { api, ApiError } from "@/lib/api";
import { getToken } from "@/lib/auth";
import { formatDateTime, formatPhone, titleCase } from "@/lib/format";
import type { AppointmentListResponse, AvailabilityResponse } from "@/lib/types";

function todayIso(): string {
  const d = new Date();
  const year = d.getFullYear();
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

export default function AppointmentsPage() {
  const [data, setData] = useState<AppointmentListResponse | null>(null);
  const [status, setStatus] = useState("");
  const [upcoming, setUpcoming] = useState(false);
  const [day, setDay] = useState(todayIso());
  const [availability, setAvailability] = useState<AvailabilityResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!getToken()) return;
    let cancelled = false;
    api
      .appointments({ status: status || undefined, upcoming })
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof ApiError ? err.message : "Failed to load appointments");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [status, upcoming]);

  useEffect(() => {
    if (!getToken() || !day) return;
    let cancelled = false;
    api
      .availability(day)
      .then((result) => {
        if (!cancelled) setAvailability(result);
      })
      .catch(() => {
        if (!cancelled) setAvailability(null);
      });
    return () => {
      cancelled = true;
    };
  }, [day]);

  if (error) return <div className="error-banner">{error}</div>;
  if (!data) return <div className="loading">Loading…</div>;

  return (
    <>
      <header className="page-head">
        <h1>Appointments</h1>
        <p className="muted">Bookings are always scoped to your tenant.</p>
      </header>

      <div className="filters">
        <select
          aria-label="Status filter"
          value={status}
          onChange={(e) => setStatus(e.target.value)}
        >
          <option value="">All statuses</option>
          <option value="pending">Pending</option>
          <option value="confirmed">Confirmed</option>
          <option value="rescheduled">Rescheduled</option>
          <option value="cancelled">Cancelled</option>
          <option value="no_show">No-show</option>
          <option value="failed">Failed</option>
        </select>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={upcoming}
            onChange={(e) => setUpcoming(e.target.checked)}
          />
          Upcoming only
        </label>
      </div>

      <section className="card">
        <h2>Appointments ({data.total})</h2>
        {data.appointments.length === 0 ? (
          <div className="empty">No appointments match.</div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Customer</th>
                  <th>Phone</th>
                  <th>Starts</th>
                  <th>Status</th>
                  <th>Provider</th>
                </tr>
              </thead>
              <tbody>
                {data.appointments.map((appointment) => (
                  <tr key={appointment.id}>
                    <td>{appointment.customer_name}</td>
                    <td>{formatPhone(appointment.customer_phone)}</td>
                    <td>{formatDateTime(appointment.starts_at)}</td>
                    <td>
                      <span className={`badge ${appointment.status}`}>
                        {titleCase(appointment.status)}
                      </span>
                    </td>
                    <td>{appointment.provider ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <h2>Availability — {day}</h2>
        <input
          aria-label="Availability date"
          type="date"
          value={day}
          onChange={(e) => setDay(e.target.value)}
        />
        {availability ? (
          <>
            {availability.degraded ? (
              <p className="error">Degraded: {availability.degraded}</p>
            ) : null}
            {availability.slots.length === 0 ? (
              <div className="empty">No slots available this day.</div>
            ) : (
              <ul className="slot-list">
                {availability.slots.map((slot) => (
                  <li key={`${slot.start}-${slot.end}`}>
                    {formatDateTime(slot.start)} – {formatDateTime(slot.end)}
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : (
          <div className="empty">Availability unavailable.</div>
        )}
      </section>
    </>
  );
}
