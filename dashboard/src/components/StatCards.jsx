/**
 * LEGACY — not mounted.
 *
 * Superseded by `pages/Overview.jsx`, which is fed by `/api/analytics/overview`.
 * Kept only so nothing that still imports it breaks; it is not reachable from
 * `App.jsx` any more. Delete once phase 2 confirms no consumer remains.
 *
 * The "Est. revenue captured" tile has been **removed rather than carried
 * over**. It rendered `estimated_value_usd`, which the server computes as
 * `booked * 150` -- a constant with no relationship to any appointment, lead
 * or invoice on the tenant's account. Presenting that as money is the single
 * most misleading thing the old dashboard did. The field still exists in the
 * `/tenants/{id}/stats` response so no API consumer breaks; it is simply not
 * displayed, and no replacement figure is invented in its place.
 */
export default function StatCards({ stats }) {
  if (!stats) return null
  const cards = [
    { label: 'Calls answered', value: stats.calls },
    { label: 'Appointments booked', value: stats.booked },
    { label: 'Booking rate', value: `${Math.round(stats.booking_rate * 100)}%` },
    { label: 'Minutes used', value: stats.minutes },
  ]
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(160px,1fr))', gap: 12 }}>
      {cards.map((c) => (
        <div key={c.label} style={{ background: '#fff', border: '1px solid #e5e7eb', borderRadius: 10, padding: 16 }}>
          <div style={{ fontSize: 12, color: '#6b7280' }}>{c.label}</div>
          <div style={{ fontSize: 26, fontWeight: 700 }}>{c.value}</div>
        </div>
      ))}
    </div>
  )
}
