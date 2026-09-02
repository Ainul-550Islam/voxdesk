/**
 * Charts.
 *
 * Hand-rolled SVG and CSS rather than a charting library. Three reasons: the
 * shapes needed here are bars and a stacked column series; a chart library is
 * ~150 KB for that; and every number rendered has to be traceable to a
 * backend field, which is easier to keep honest when the markup is visible.
 *
 * Every chart is also readable without seeing it: each carries a `<table>`
 * behind `.sr-only`, so a screen reader gets the data rather than "graphic".
 */
import { formatNumber, formatPercent } from '../lib/format'

/** A labelled horizontal bar list. */
export function BarList({ items, total, formatValue = formatNumber, ariaLabel }) {
  const max = Math.max(total ?? 0, ...items.map((item) => item.value), 1)

  return (
    <>
      <div className="bars" aria-hidden="true">
        {items.map((item) => (
          <div className="bar-row" key={item.label}>
            <div className="bar-row__label">{item.label}</div>
            <div className="bar-row__track">
              <div
                className="bar-row__fill"
                style={{
                  width: `${(item.value / max) * 100}%`,
                  background: item.color ?? 'var(--brand)',
                }}
              />
            </div>
            <div className="bar-row__value">{formatValue(item.value)}</div>
          </div>
        ))}
      </div>
      <table className="sr-only">
        <caption>{ariaLabel}</caption>
        <tbody>
          {items.map((item) => (
            <tr key={item.label}>
              <th scope="row">{item.label}</th>
              <td>{formatValue(item.value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

/**
 * A stacked daily column chart.
 *
 * Gap-filled by the server, so a quiet Saturday shows as a zero column rather
 * than the chart drawing a straight line through it.
 */
export function DailyColumns({ series, ariaLabel }) {
  const max = Math.max(
    1,
    ...series.map((point) => point.answered + point.missed + point.failed)
  )

  return (
    <>
      <div className="spark" aria-hidden="true">
        {series.map((point) => {
          const scale = (value) => (value / max) * 100
          return (
            <div
              className="spark__col"
              key={point.date}
              title={`${point.date}: ${point.answered} answered, ${point.missed} missed, ${point.failed} failed`}
            >
              <div
                className="spark__seg spark__seg--failed"
                style={{ height: `${scale(point.failed)}%` }}
              />
              <div
                className="spark__seg spark__seg--missed"
                style={{ height: `${scale(point.missed)}%` }}
              />
              <div
                className="spark__seg spark__seg--answered"
                style={{ height: `${scale(point.answered)}%` }}
              />
            </div>
          )
        })}
      </div>
      <div className="legend" aria-hidden="true">
        <span>
          <i className="legend__swatch" style={{ background: 'var(--brand)' }} />
          Answered
        </span>
        <span>
          <i className="legend__swatch" style={{ background: '#fbbf24' }} />
          Missed
        </span>
        <span>
          <i className="legend__swatch" style={{ background: '#f87171' }} />
          Failed
        </span>
      </div>
      <table className="sr-only">
        <caption>{ariaLabel}</caption>
        <thead>
          <tr>
            <th scope="col">Date</th>
            <th scope="col">Answered</th>
            <th scope="col">Missed</th>
            <th scope="col">Failed</th>
          </tr>
        </thead>
        <tbody>
          {series.map((point) => (
            <tr key={point.date}>
              <th scope="row">{point.date}</th>
              <td>{point.answered}</td>
              <td>{point.missed}</td>
              <td>{point.failed}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

/**
 * A conversion funnel.
 *
 * Each stage shows its count, its share of the stage above, and the server's
 * own one-line definition of what it counts — because a funnel stage nobody
 * can define is a number nobody can defend.
 */
export function Funnel({ stages }) {
  const top = Math.max(1, stages[0]?.count ?? 1)

  return (
    <div>
      {stages.map((stage, index) => {
        const previous = index === 0 ? null : stages[index - 1].count
        const share = previous ? (stage.count / previous) * 100 : null
        return (
          <div key={stage.stage}>
            <div className="funnel__row">
              <div className="bar-row__label" title={stage.note}>
                {stage.stage}
              </div>
              <div
                className="funnel__bar"
                style={{ width: `${Math.max(1, (stage.count / top) * 100)}%` }}
                aria-hidden="true"
              />
              <div className="bar-row__value">{formatNumber(stage.count)}</div>
            </div>
            <div
              className="stat__hint"
              style={{ marginLeft: 140, marginTop: -4, marginBottom: 8 }}
            >
              {stage.note}
              {share !== null && ` — ${formatPercent(share, 0)} of previous`}
            </div>
          </div>
        )
      })}
    </div>
  )
}

/** A usage meter with an explicit over-limit state. */
export function UsageMeter({ label, used, included, unit, percent, overage }) {
  const capped = Math.min(100, percent ?? 0)
  const over = (overage ?? 0) > 0
  const tone = over ? 'var(--danger)' : capped >= 80 ? 'var(--warn)' : 'var(--brand)'

  return (
    <div style={{ marginBottom: 14 }}>
      <div className="row" style={{ justifyContent: 'space-between' }}>
        <span style={{ fontWeight: 600 }}>{label}</span>
        <span className="muted">
          {formatNumber(used)} / {included ? formatNumber(included) : '—'} {unit}
          {included ? '' : ' (not included in plan)'}
        </span>
      </div>
      <div
        className="bar-row__track"
        style={{ height: 10, marginTop: 6 }}
        role="meter"
        aria-valuenow={Math.round(percent ?? 0)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${label} usage`}
      >
        <div
          className="bar-row__fill"
          style={{ width: `${capped}%`, background: tone }}
        />
      </div>
      {over && (
        <div className="stat__hint" style={{ color: 'var(--danger)' }}>
          {formatNumber(overage)} {unit} over the included allowance
        </div>
      )}
    </div>
  )
}

