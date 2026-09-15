/**
 * LEGACY — not mounted.
 *
 * Superseded by `pages/Calls.jsx`, which paginates and filters server-side and
 * links each row to `/calls/:id`. Kept only so nothing that still imports it
 * breaks.
 *
 * The two formatting defects the audit raised (F11 browser-timezone
 * timestamps, F12 raw `1320s` durations) are fixed here too by delegating to
 * the shared `lib/format` helpers, so the file cannot be re-mounted and
 * reintroduce them. It now requires a `timezone`.
 */
import { formatDateTime, formatDuration } from '../lib/format'

export default function CallTable({ calls, timezone, onSelect }) {
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse' }}>
      <thead>
        <tr>
          <th scope="col">When</th>
          <th scope="col">Duration</th>
          <th scope="col">Status</th>
          <th scope="col">Booked</th>
          <th scope="col">Transcript</th>
        </tr>
      </thead>
      <tbody>
        {calls.map((c) => (
          <tr key={c.id}>
            <td>{formatDateTime(c.started_at, timezone)}</td>
            <td>{formatDuration(c.duration)}</td>
            <td>{c.status}</td>
            <td>{c.booked ? 'Yes' : 'No'}</td>
            <td>
              <button type="button" onClick={() => onSelect(c.id)}>View</button>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}