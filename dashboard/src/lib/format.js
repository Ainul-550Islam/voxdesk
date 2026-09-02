/**
 * Formatting.
 *
 * Centralised because the audit found every one of these done inline and
 * wrongly: durations rendered as `1320s`, timestamps in the *browser's*
 * timezone rather than the business's, and money interpolated with a
 * hard-coded `$`.
 *
 * The rule for time: **backend timestamps are UTC; render them in the
 * tenant's timezone.** A US business viewed from Dhaka must show US call
 * times, or the operator reads every row wrong.
 */

/** A safe IANA zone, falling back rather than throwing on a bad tenant row. */
export function safeZone(timeZone) {
  if (!timeZone) return 'UTC'
  try {
    new Intl.DateTimeFormat('en-US', { timeZone })
    return timeZone
  } catch {
    return 'UTC'
  }
}

function parse(value) {
  if (!value) return null
  // A backend timestamp without an offset is UTC by convention (SQLite hands
  // back naive values even for timezone-aware columns). Assuming local here
  // is how a whole dashboard silently shifts by hours.
  const text =
    typeof value === 'string' && !/[Zz]|[+-]\d{2}:?\d{2}$/.test(value)
      ? `${value}Z`
      : value
  const date = new Date(text)
  return Number.isNaN(date.getTime()) ? null : date
}

export function formatDateTime(value, timeZone, options = {}) {
  const date = parse(value)
  if (!date) return '—'
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: safeZone(timeZone),
    year: 'numeric', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hour12: false,
    ...options,
  }).format(date)
}

export function formatDate(value, timeZone) {
  const date = parse(value)
  if (!date) return '—'
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: safeZone(timeZone),
    year: 'numeric', month: 'short', day: '2-digit',
  }).format(date)
}

export function formatTime(value, timeZone) {
  const date = parse(value)
  if (!date) return '—'
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: safeZone(timeZone),
    hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date)
}

/** "3 minutes ago", "in 2 hours". Falls back to an absolute date past a week. */
export function formatRelative(value, timeZone) {
  const date = parse(value)
  if (!date) return '—'
  const seconds = (date.getTime() - Date.now()) / 1000
  const abs = Math.abs(seconds)
  if (abs > 7 * 86400) return formatDate(value, timeZone)

  const rtf = new Intl.RelativeTimeFormat('en', { numeric: 'auto' })
  const units = [
    ['day', 86400], ['hour', 3600], ['minute', 60], ['second', 1],
  ]
  for (const [unit, size] of units) {
    if (abs >= size || unit === 'second') {
      return rtf.format(Math.round(seconds / size), unit)
    }
  }
  return formatDate(value, timeZone)
}

/**
 * Seconds as a human duration.
 *
 * The audit found `Math.round(c.duration)}s`, so a 22-minute call read
 * `1320s`. Nobody counts in kiloseconds.
 */
export function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0))
  if (total === 0) return '0s'
  if (total < 60) return `${total}s`
  const minutes = Math.floor(total / 60)
  const rest = total % 60
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes}m`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

/** Minutes, for usage figures that are already in minutes. */
export function formatMinutes(minutes) {
  const value = Number(minutes) || 0
  return value >= 100 ? Math.round(value).toLocaleString('en-US')
    : value.toLocaleString('en-US', { maximumFractionDigits: 1 })
}

export function formatNumber(value) {
  return (Number(value) || 0).toLocaleString('en-US')
}

/**
 * A percentage the backend already expressed as 0–100.
 *
 * Never multiplies. The server owns every rate so that the numerator and
 * denominator are defined in one place; a browser that also multiplied would
 * produce 2000%.
 */
export function formatPercent(value, digits = 1) {
  const number = Number(value)
  if (!Number.isFinite(number)) return '—'
  return `${number.toFixed(digits).replace(/\.0$/, '')}%`
}

/**
 * Minor units in the tenant's billing currency.
 *
 * `Intl` supplies the symbol, so a tenant billed in EUR does not see a dollar
 * sign. The audit found `$${...}` hard-coded.
 */
export function formatMoney(cents, currency = 'usd') {
  const amount = (Number(cents) || 0) / 100
  try {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: (currency || 'usd').toUpperCase(),
      minimumFractionDigits: 2,
    }).format(amount)
  } catch {
    return `${amount.toFixed(2)} ${(currency || 'usd').toUpperCase()}`
  }
}

/** A phone number, lightly. Never reformats an unrecognised shape. */
export function formatPhone(value) {
  if (!value) return '—'
  const digits = String(value).replace(/[^\d]/g, '')
  if (digits.length === 11 && digits.startsWith('1')) {
    return `+1 (${digits.slice(1, 4)}) ${digits.slice(4, 7)}-${digits.slice(7)}`
  }
  if (digits.length === 10) {
    return `(${digits.slice(0, 3)}) ${digits.slice(3, 6)}-${digits.slice(6)}`
  }
  return value
}

/** `snake_case` or `SCREAMING_CASE` into something readable. */
export function humanise(value) {
  if (!value) return '—'
  return String(value)
    .replace(/[_.]/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
}

/** Today in the tenant's timezone, as `YYYY-MM-DD`. */
export function todayIn(timeZone) {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: safeZone(timeZone),
    year: 'numeric', month: '2-digit', day: '2-digit',
  }).format(new Date())
  return parts
}
