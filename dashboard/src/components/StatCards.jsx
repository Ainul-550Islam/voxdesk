export default function StatCards({ stats }) {
  if (!stats) return null
  const cards = [
    { label: 'Calls answered', value: stats.calls },
    { label: 'Appointments booked', value: stats.booked },
    { label: 'Booking rate', value: `${Math.round(stats.booking_rate * 100)}%` },
    { label: 'Minutes used', value: stats.minutes },
    { label: 'Est. revenue captured', value: `$${stats.estimated_value_usd}` },
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
