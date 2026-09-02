/**
 * Range presets.
 *
 * Names only. The dates behind them are computed server-side in the tenant's
 * timezone — see `DateRange.jsx` and `app/api/analytics_routes.resolve_window`.
 */
export const PRESETS = [
  { value: 'today', label: 'Today' },
  { value: 'yesterday', label: 'Yesterday' },
  { value: 'last_7_days', label: 'Last 7 days' },
  { value: 'last_30_days', label: 'Last 30 days' },
  { value: 'this_month', label: 'This month' },
  { value: 'previous_month', label: 'Previous month' },
]

export const DEFAULT_RANGE = { range: 'last_30_days' }
