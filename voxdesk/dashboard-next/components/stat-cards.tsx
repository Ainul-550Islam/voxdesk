import { titleCase } from "@/lib/format";

const LABELS: Record<string, string> = {
  total: "Total calls",
  answered: "Answered",
  missed: "Missed",
  failed: "Failed",
  inbound: "Inbound",
  outbound: "Outbound",
  transferred: "Transferred",
  minutes: "Minutes",
};

export default function StatCards({
  stats,
  title,
}: {
  stats: Record<string, unknown>;
  title: string;
}) {
  const entries = Object.entries(stats);

  return (
    <section>
      {entries.length === 0 ? (
        <div className="card empty">{title}: no data for this window.</div>
      ) : (
        <div className="stat-grid">
          {entries.map(([key, value]) => (
            <div className="stat" key={key}>
              <div className="value">{String(value)}</div>
              <div className="label">{LABELS[key] ?? titleCase(key)}</div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
