export default function CallTable({ calls, onSelect }) {
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', background: '#fff', fontSize: 14 }}>
      <thead>
        <tr style={{ textAlign: 'left', color: '#6b7280', borderBottom: '1px solid #e5e7eb' }}>
          <th style={{ padding: 10 }}>From</th>
          <th>When</th>
          <th>Length</th>
          <th>Outcome</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {calls.map((c) => (
          <tr key={c.id} style={{ borderBottom: '1px solid #f3f4f6' }}>
            <td style={{ padding: 10 }}>{c.from}</td>
            <td>{new Date(c.started_at).toLocaleString()}</td>
            <td>{Math.round(c.duration)}s</td>
            <td>
              {c.booked && <span style={{ color: '#059669' }}>Booked</span>}
              {c.escalated && <span style={{ color: '#d97706' }}>Escalated</span>}
              {!c.booked && !c.escalated && <span style={{ color: '#6b7280' }}>{c.intent || '—'}</span>}
            </td>
            <td><button onClick={() => onSelect(c.id)}>Transcript</button></td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
