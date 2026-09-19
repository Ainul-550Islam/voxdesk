/**
 * The API client.
 *
 * Everything that talks to the backend goes through `request()`. That is the
 * point of the file: the audit found fetch logic inlined in `App.jsx` with no
 * shared error handling, so the second page would have re-invented it.
 *
 * **Token storage.** The access token lives in a module-level variable —
 * never localStorage, never sessionStorage, never a cookie JavaScript can
 * read. Anything reachable from JS is reachable from an XSS payload. The cost
 * is that a refresh loses it, which is exactly why the refresh token is an
 * HttpOnly cookie: `bootstrap()` trades it for a new access token on load.
 * This was already right before STEP 8 and is carried forward unchanged.
 *
 * **One retry, then out.** A 401 rotates the token once and replays. If that
 * fails we log out. There is no path that can loop.
 */

const BASE = '/api'

let accessToken = null
let onUnauthorized = () => {}

export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn
}

export function isAuthenticated() {
  return accessToken !== null
}

function clearToken() {
  accessToken = null
}

export class ApiError extends Error {
  constructor(status, message, detail = null) {
    super(message)
    this.status = status
    this.detail = detail
  }

  /** Whether retrying the same request could plausibly succeed. */
  get retryable() {
    return this.status === 0 || this.status === 429 || this.status >= 500
  }
}

/**
 * Human-facing text for a status code.
 *
 * Requirement 21 and 22: never show a raw backend exception. The server's
 * `detail` is used when it is a plain string, because FastAPI validation
 * messages are written for people; a dict detail is a structured error and is
 * summarised instead of stringified.
 */
function friendlyMessage(status, detail) {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') {
    return detail.message
  }
  return {
    0: 'Could not reach the server. Check your connection and try again.',
    400: 'That request could not be processed.',
    401: 'Your session has expired. Please sign in again.',
    403: 'You do not have permission to do that.',
    404: 'That item could not be found.',
    409: 'That conflicts with something that already exists.',
    422: 'Some of the details were not valid.',
    429: 'Too many requests. Please wait a moment and try again.',
    502: 'A third-party service is unavailable right now.',
    503: 'The service is temporarily unavailable.',
    504: 'That took too long. Please try again.',
  }[status] || 'Something went wrong. Please try again.'
}

async function readError(resp) {
  try {
    const data = await resp.json()
    return data.detail ?? null
  } catch {
    return null
  }
}

async function request(path, { method = 'GET', body, retry = true, raw } = {}) {
  const headers = {}
  if (!raw) headers['Content-Type'] = 'application/json'
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`

  let resp
  try {
    resp = await fetch(path, {
      method,
      headers,
      credentials: 'same-origin',      // carries the HttpOnly refresh cookie
      body: raw ?? (body === undefined ? undefined : JSON.stringify(body)),
    })
  } catch {
    // Network failure, DNS, offline. Status 0 so callers can offer a retry
    // rather than showing "undefined".
    throw new ApiError(0, friendlyMessage(0))
  }

  // An expired 15-minute access token is normal. Rotate once, then replay.
  if (resp.status === 401 && retry && accessToken) {
    const renewed = await tryRefresh()
    if (renewed) return request(path, { method, body, raw, retry: false })
    clearToken()
    onUnauthorized()
    throw new ApiError(401, friendlyMessage(401))
  }

  if (resp.status === 401) {
    clearToken()
    onUnauthorized()
    throw new ApiError(401, friendlyMessage(401))
  }

  if (!resp.ok) {
    const detail = await readError(resp)
    throw new ApiError(resp.status, friendlyMessage(resp.status, detail), detail)
  }

  return resp.status === 204 ? null : resp.json()
}

/** Append only the query parameters that have a value. */
export function query(params = {}) {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.set(key, String(value))
  }
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ''
}

// --------------------------------------------------------------- session ---

export async function login(email, password) {
  const resp = await fetch('/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify({ email, password }),
  })
  if (!resp.ok) {
    // The server deliberately returns one generic message; do not embellish
    // it here or we would reintroduce user enumeration in the UI.
    const detail = await readError(resp)
    throw new ApiError(resp.status, friendlyMessage(resp.status, detail))
  }
  const data = await resp.json()
  accessToken = data.access_token
  return data.user
}

async function tryRefresh() {
  try {
    const resp = await fetch('/auth/refresh', {
      method: 'POST',
      credentials: 'same-origin',
    })
    if (!resp.ok) return false
    accessToken = (await resp.json()).access_token
    return true
  } catch {
    return false
  }
}

/**
 * The current in-memory access token, for the one consumer allowed to see it
 * beyond `request()`: the realtime WebSocket client, which must put the token
 * in its hello frame (browsers cannot set headers on a WebSocket). There is
 * deliberately NO setter export — the token stays write-private to this
 * module, so the only writers remain login/refresh/logout.
 */
export function getAccessToken() {
  return accessToken
}

/**
 * Trade the HttpOnly refresh cookie for a new access token, for consumers
 * outside the request() retry loop (again: the realtime socket, when the
 * gateway closes an expired-token connection with 1008). Returns false when
 * the session is genuinely over — callers must NOT call onUnauthorized
 * themselves; the next failed API request already does that once.
 */
export async function refreshAccessToken() {
  return tryRefresh()
}

/** On page load, exchange the HttpOnly refresh cookie for a session. */
export async function bootstrap() {
  if (!(await tryRefresh())) return null
  try {
    return await request('/auth/me')
  } catch {
    clearToken()
    return null
  }
}

export async function logout() {
  try {
    await request('/auth/logout', { method: 'POST' })
  } catch {
    // A failed logout must still clear the local session. The refresh cookie
    // is HttpOnly and the server will reject a stale one anyway.
  } finally {
    clearToken()
    onUnauthorized()
  }
}

export const getMe = () => request('/auth/me')

// ------------------------------------------------------------- analytics ---
//
// `range` is a preset name or `start`/`end` are local dates. The *server*
// resolves both against the tenant's timezone -- requirement 5 forbids doing
// period arithmetic in the browser, and two people in different timezones
// must see the same dashboard.

export const getOverview = (range) =>
  request(`${BASE}/analytics/overview${query(range)}`)
export const getCallAnalytics = (range) =>
  request(`${BASE}/analytics/calls${query(range)}`)
export const getConversion = (range) =>
  request(`${BASE}/analytics/conversion${query(range)}`)
export const getUsageAnalytics = () => request(`${BASE}/analytics/usage`)

// ----------------------------------------------------------------- calls ---

export const listCalls = (tenantId, params) =>
  request(`${BASE}/tenants/${tenantId}/calls${query(params)}`)
export const getCall = (callId) => request(`${BASE}/calls/${callId}`)
export const getTranscript = (callId) => request(`${BASE}/calls/${callId}/transcript`)
export const getTransfer = (callId) => request(`${BASE}/calls/${callId}/transfer`)

// ----------------------------------------------------------------- leads ---

export const listLeads = (tenantId, params) =>
  request(`${BASE}/tenants/${tenantId}/leads${query(params)}`)
export const createLeads = (tenantId, leads) =>
  request(`${BASE}/tenants/${tenantId}/leads`, { method: 'POST', body: { leads } })
export const markLeadDnc = (tenantId, leadId) =>
  request(`${BASE}/tenants/${tenantId}/leads/${leadId}/do-not-call`, { method: 'POST' })

// ---------------------------------------------------------- appointments ---

export const listAppointments = (params) =>
  request(`${BASE}/appointments${query(params)}`)
export const getAppointment = (id) => request(`${BASE}/appointments/${id}`)
export const cancelAppointment = (id, reason) =>
  request(`${BASE}/appointments/${id}/cancel`, { method: 'POST', body: { reason } })
export const rescheduleAppointment = (id, startsAtLocal, reason = '') =>
  request(`${BASE}/appointments/${id}`, {
    method: 'PATCH',
    body: { starts_at_local: startsAtLocal, reason },
  })
export const getAvailability = (day) =>
  request(`${BASE}/appointments/availability${query({ day })}`)

// ------------------------------------------------------------- campaigns ---

export const listCampaigns = (tenantId) =>
  request(`${BASE}/tenants/${tenantId}/campaigns`)
export const runCampaign = (tenantId, campaignId) =>
  request(`${BASE}/tenants/${tenantId}/campaigns/${campaignId}/run`, { method: 'POST' })

// ------------------------------------------------------------- knowledge ---

export const listDocuments = (params) =>
  request(`${BASE}/knowledge/documents${query(params)}`)
export const getDocument = (id) => request(`${BASE}/knowledge/documents/${id}`)
export const knowledgeStats = () => request(`${BASE}/knowledge/stats`)
export const reindexDocument = (id) =>
  request(`${BASE}/knowledge/documents/${id}/reindex`, { method: 'POST' })
export const restoreDocument = (id) =>
  request(`${BASE}/knowledge/documents/${id}/restore`, { method: 'POST' })
export const deleteDocument = (id, hard = false) =>
  request(`${BASE}/knowledge/documents/${id}${query({ hard })}`, { method: 'DELETE' })

export function uploadDocument(file, title) {
  // Multipart, so `request` must not set a JSON content type -- the browser
  // has to supply its own boundary.
  const form = new FormData()
  form.append('file', file)
  if (title) form.append('title', title)
  return request(`${BASE}/knowledge/documents`, { method: 'POST', raw: form })
}

// ---------------------------------------------------------- integrations ---

export const listCrmIntegrations = () => request(`${BASE}/integrations/crm`)
export const crmProviders = () => request(`${BASE}/integrations/crm/providers`)
export const listCrmSyncs = (params) =>
  request(`${BASE}/integrations/crm/syncs${query(params)}`)
export const testCrmIntegration = (provider) =>
  request(`${BASE}/integrations/crm/${provider}/test`, { method: 'POST' })
export const saveCrmIntegration = (provider, body) =>
  request(`${BASE}/integrations/crm/${provider}`, { method: 'PUT', body })
export const disconnectCrmIntegration = (provider) =>
  request(`${BASE}/integrations/crm/${provider}/disconnect`, { method: 'POST' })
// Distinct from disconnect: DELETE removes the row, disconnect only drops
// the credentials and keeps the configuration. Returns 204.
export const deleteCrmIntegration = (provider) =>
  request(`${BASE}/integrations/crm/${provider}`, { method: 'DELETE' })

export const listCalendarIntegrations = () => request(`${BASE}/calendar/integrations`)
export const calendarProviders = () => request(`${BASE}/calendar/providers`)
export const testCalendarIntegration = (provider) =>
  request(`${BASE}/calendar/integrations/${provider}/test`, { method: 'POST' })
export const saveCalendarIntegration = (provider, body) =>
  request(`${BASE}/calendar/integrations/${provider}`, { method: 'PUT', body })
export const deleteCalendarIntegration = (provider) =>
  request(`${BASE}/calendar/integrations/${provider}`, { method: 'DELETE' })
export const getSchedulingPolicy = () => request(`${BASE}/calendar/policy`)
export const saveSchedulingPolicy = (body) =>
  request(`${BASE}/calendar/policy`, { method: 'PUT', body })

// --------------------------------------------------------------- billing ---

export const getBilling = () => request(`${BASE}/billing`)
export const getPlans = () => request(`${BASE}/billing/plans`)
export const getUsage = () => request(`${BASE}/billing/usage`)
export const getInvoices = () => request(`${BASE}/billing/invoices`)
export const startCheckout = (planCode, interval = 'month') =>
  request(`${BASE}/billing/checkout`, {
    method: 'POST',
    // Plan code and interval only. There is no price, amount or currency
    // field -- the server resolves the price from its catalogue, and the
    // request model would reject anything else anyway.
    body: { plan_code: planCode, interval },
  })
export const openPortal = () => request(`${BASE}/billing/portal`, { method: 'POST' })
export const changePlan = (planCode, interval) =>
  request(`${BASE}/billing/change-plan`, {
    method: 'POST',
    body: { plan_code: planCode, interval },
  })
export const cancelSubscription = (immediately, reason) =>
  request(`${BASE}/billing/cancel`, {
    method: 'POST',
    body: { immediately, reason },
  })
// Re-reads the provider and rebuilds the usage summary; returns the refreshed
// BillingStatusOut. No body -- the tenant comes from the token.
export const reconcileBilling = () =>
  request(`${BASE}/billing/reconcile`, { method: 'POST' })

// ------------------------------------------------------------------ team ---

export const listUsers = () => request(`${BASE}/team/users`)
// The RBAC policy itself (`describe_roles()`): every role with its level and
// full permission list, so the dashboard never hard-codes the role table.
export const getRoles = () => request('/auth/roles')
export const createUser = (body) =>
  request(`${BASE}/team/users`, { method: 'POST', body })
export const setUserRole = (userId, role) =>
  request(`${BASE}/team/users/${userId}/role`, { method: 'PATCH', body: { role } })
export const setUserActive = (userId, isActive) =>
  request(`${BASE}/team/users/${userId}/active`, {
    method: 'PATCH',
    body: { is_active: isActive },
  })
export const listAudit = (params) => request(`${BASE}/team/audit${query(params)}`)

// ------------------------------------------------------------------ agent ---
//
// `getAgentConfig` is the read half of an API that was previously write-only:
// `PATCH .../voice` has always existed, but nothing could read the greeting,
// the prompt or the model back. The response never contains a credential --
// see `AgentConfigOut` in `app/api/routes.py`.

export const getAgentConfig = (tenantId) =>
  request(`${BASE}/tenants/${tenantId}/agent`)
export const listTenants = () => request(`${BASE}/tenants`)
export const listLanguages = () => request(`${BASE}/languages`)
export const listPresets = () => request(`${BASE}/llm/presets`)
export const updateVoice = (tenantId, body) =>
  request(`${BASE}/tenants/${tenantId}/voice`, { method: 'PATCH', body })