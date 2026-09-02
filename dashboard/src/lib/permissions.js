/**
 * Frontend permission helpers.
 *
 * **These are UX, not security.** Hiding a button stops a manager from
 * clicking something that would 403; it does not stop anyone from calling the
 * API. Every one of these permissions is enforced again server-side by
 * `require_permission`, and that is the boundary that matters. The tests
 * assert both halves.
 *
 * Centralised so nothing writes `user.role === 'admin'` in a component --
 * requirement 20, and the reason is that a role check scattered across
 * twenty files cannot be changed when the policy does.
 */

export const PERMISSIONS = {
  CALL_READ: 'call:read',
  TRANSCRIPT_READ: 'transcript:read',
  RECORDING_READ: 'recording:read',
  LEAD_READ: 'lead:read',
  LEAD_CREATE: 'lead:create',
  LEAD_UPDATE: 'lead:update',
  APPOINTMENT_READ: 'appointment:read',
  APPOINTMENT_WRITE: 'appointment:write',
  CAMPAIGN_READ: 'campaign:read',
  CAMPAIGN_RUN: 'campaign:run',
  KNOWLEDGE_READ: 'knowledge:read',
  KNOWLEDGE_WRITE: 'knowledge:write',
  KNOWLEDGE_DELETE: 'knowledge:delete',
  ANALYTICS_READ: 'analytics:read',
  AUDIT_READ: 'audit:read',
  INTEGRATION_READ: 'integration:read',
  INTEGRATION_WRITE: 'integration:write',
  BILLING_READ: 'billing:read',
  BILLING_WRITE: 'billing:write',
  USER_READ: 'user:read',
  USER_CREATE: 'user:create',
  USER_UPDATE: 'user:update',
  TENANT_UPDATE: 'tenant:update',
}

/**
 * Build a checker from `/auth/me`.
 *
 * The permission list comes from the server's own RBAC policy, so the UI
 * cannot drift from it the way a hard-coded role matrix would.
 */
export function makeCan(permissions) {
  const granted = new Set(Array.isArray(permissions) ? permissions : [])
  const can = (permission) => granted.has(permission)
  can.any = (...list) => list.some((p) => granted.has(p))
  can.all = (...list) => list.every((p) => granted.has(p))
  return can
}

/** A `can` that grants nothing. Used before `/auth/me` has resolved. */
export const denyAll = makeCan([])
