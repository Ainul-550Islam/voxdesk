import { titleCase } from "@/lib/format";

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (Array.isArray(value)) return value.length === 0 ? "—" : value.join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

// Reusable key/value section. Values are rendered defensively: booleans as
// Yes/No, arrays joined, objects stringified — nothing here trusts a shape.
export default function KvList({
  title,
  entries,
}: {
  title: string;
  entries: Record<string, unknown>;
}) {
  const items = Object.entries(entries);

  return (
    <section className="card">
      <h2>{title}</h2>
      {items.length === 0 ? (
        <div className="empty">No data.</div>
      ) : (
        <dl className="kv">
          {items.map(([key, value]) => (
            <div key={key}>
              <dt>{titleCase(key)}</dt>
              <dd>{formatValue(value)}</dd>
            </div>
          ))}
        </dl>
      )}
    </section>
  );
}
