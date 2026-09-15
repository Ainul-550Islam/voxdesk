import { formatDateTime, formatDuration, formatPhone, titleCase } from "@/lib/format";
import type { CallItem } from "@/lib/types";

export default function CallsTable({ calls }: { calls: CallItem[] }) {
  if (calls.length === 0) {
    return <div className="empty">No calls match these filters.</div>;
  }

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>From</th>
            <th>To</th>
            <th>Direction</th>
            <th>Started</th>
            <th>Duration</th>
            <th>Status</th>
            <th>Outcome</th>
          </tr>
        </thead>
        <tbody>
          {calls.map((call) => (
            <tr key={call.id}>
              <td>{formatPhone(call.from)}</td>
              <td>{formatPhone(call.to)}</td>
              <td>{call.direction}</td>
              <td>{formatDateTime(call.started_at)}</td>
              <td>{formatDuration(call.duration)}</td>
              <td>
                <span className={`badge ${call.status}`}>{titleCase(call.status)}</span>
              </td>
              <td>
                {call.booked ? <span className="badge completed">Booked</span> : null}{" "}
                {call.escalated ? (
                  <span className="badge transferred">Transferred</span>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
