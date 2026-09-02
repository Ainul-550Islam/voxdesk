/**
 * The shared analytics range control.
 *
 * Requirement 5. The presets are sent to the server **by name**; the server
 * resolves them against the tenant's timezone and returns the resolved window
 * with the response.
 *
 * That direction matters. If the browser computed "today" and sent dates, a
 * business in Los Angeles viewed from Dhaka would see a different "today"
 * than the same business viewed from New York — two people looking at one
 * dashboard, disagreeing. The server owns the calendar.
 */
import { PRESETS } from '../lib/ranges'

export default function DateRange({ value, onChange, timezone }) {
  const isCustom = value.range === undefined && value.start

  return (
    <div className="row" role="group" aria-label="Date range">
      <label className="sr-only" htmlFor="range-preset">Date range</label>
      <select
        id="range-preset"
        className="select"
        style={{ width: 'auto' }}
        value={isCustom ? 'custom' : value.range ?? 'last_30_days'}
        onChange={(event) => {
          const next = event.target.value
          if (next === 'custom') {
            const today = new Date().toISOString().slice(0, 10)
            onChange({ start: today, end: today })
          } else {
            onChange({ range: next })
          }
        }}
      >
        {PRESETS.map((preset) => (
          <option key={preset.value} value={preset.value}>{preset.label}</option>
        ))}
        <option value="custom">Custom range…</option>
      </select>

      {isCustom && (
        <>
          <label className="sr-only" htmlFor="range-start">Start date</label>
          <input
            id="range-start" type="date" className="input"
            style={{ width: 'auto' }} value={value.start ?? ''}
            max={value.end}
            onChange={(event) =>
              onChange({ start: event.target.value, end: value.end })}
          />
          <span aria-hidden="true" className="muted">→</span>
          <label className="sr-only" htmlFor="range-end">End date</label>
          <input
            id="range-end" type="date" className="input"
            style={{ width: 'auto' }} value={value.end ?? ''}
            min={value.start}
            onChange={(event) =>
              onChange({ start: value.start, end: event.target.value })}
          />
        </>
      )}

      {timezone && (
        <span className="muted" style={{ fontSize: 12 }}>
          Times shown in {timezone}
        </span>
      )}
    </div>
  )
}

