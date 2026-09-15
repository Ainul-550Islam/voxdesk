// Types mirrored from the FastAPI backend response models so the Next.js
// dashboard and the API can never drift silently. See app/api/auth_routes.py,
// app/api/analytics_routes.py, app/api/routes.py, app/api/knowledge_routes.py,
// app/api/integration_routes.py, app/api/team_routes.py, app/api/
// billing_routes.py, app/api/appointment_routes.py and app/api/
// campaign_routes.py for the source of truth.

export type Role = "owner" | "admin" | "manager" | "agent" | "viewer";

export type CallStatus =
  | "ringing"
  | "in_progress"
  | "completed"
  | "failed"
  | "no_answer"
  | "transferred";

export type CallDirection = "inbound" | "outbound";

export type TransferState =
  | "none"
  | "requested"
  | "dialing"
  | "connected"
  | "failed";

export type Speaker = "user" | "assistant" | "system";

/** Safe public user projection (mirrors app.api.auth_routes.UserOut). */
export interface User {
  id: string;
  email: string;
  full_name: string;
  role: Role;
  tenant_id: string;
  is_active: boolean;
  created_at: string | null;
  last_login_at: string | null;
}

/** Mirrors app.api.auth_routes.TokenOut. */
export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  user: User;
}

/** Mirrors app.api.auth_routes.MeOut. */
export interface MeResponse {
  user: User;
  tenant: Record<string, unknown>;
  permissions: string[];
}

/** Mirrors app.api.analytics_routes.OverviewOut. */
export interface OverviewResponse {
  window: { start: string; end: string } | null;
  calls: Record<string, unknown>;
  conversion: Record<string, unknown>;
  operations: Record<string, unknown>;
  integrations: Record<string, unknown>;
  usage: Record<string, unknown> | null;
}

/** One row of GET /api/tenants/{tenant_id}/calls (the `calls` list). */
export interface CallItem {
  id: string;
  from: string;
  to: string;
  direction: CallDirection;
  started_at: string | null;
  duration: number;
  status: CallStatus;
  intent: string | null;
  booked: boolean;
  escalated: boolean;
  transfer_state: TransferState;
  summary: string | null;
  llm_used: string | null;
  has_recording: boolean;
  lead_id: string | null;
}

/** The calls envelope returned by the list endpoint. */
export interface CallListResponse {
  calls: CallItem[];
  total: number;
  limit: number;
  offset: number;
}

/** GET /api/calls/{call_id} (mirrors the dict in app.api.routes). */
export interface CallDetail {
  id: string;
  call_sid: string;
  direction: CallDirection;
  status: CallStatus;
  from: string;
  to: string;
  started_at: string | null;
  ended_at: string | null;
  duration: number;
  intent: string | null;
  summary: string | null;
  booked: boolean;
  escalated: boolean;
  lead_score: number | null;
  llm_used: string | null;
  transfer: Record<string, unknown>;
  recording_url: string | null;
}

/** One turn of GET /api/calls/{call_id}/transcript. */
export interface TranscriptTurn {
  speaker: Speaker;
  text: string;
  at: string | null;
}

/** Mirrors app.api.knowledge_routes.DocumentOut. */
export interface KnowledgeDocument {
  id: string;
  title: string;
  status: string;
  source_type: string;
  original_filename: string | null;
  file_type: string | null;
  file_size: number;
  version: number;
  chunk_count: number;
  char_count: number;
  token_estimate: number;
  embedding_model: string | null;
  embedding_dimensions: number | null;
  error_message: string | null;
  metadata: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
  indexed_at: string | null;
  is_searchable: boolean;
}

/** Mirrors app.api.knowledge_routes.DocumentListOut. */
export interface DocumentListResponse {
  documents: KnowledgeDocument[];
  total: number;
  limits: Record<string, unknown>;
}

/** Mirrors app.api.knowledge_routes.SearchHit. */
export interface SearchHit {
  document_id: string;
  chunk_id: string;
  title: string;
  text: string;
  score: number;
  page: number | null;
  heading: string | null;
}

/** Mirrors app.api.knowledge_routes.SearchResponse. */
export interface SearchResponse {
  query: string;
  results: SearchHit[];
  count: number;
}

/** Mirrors GET /api/knowledge/stats. */
export interface KnowledgeStats {
  documents: Record<string, number>;
  total_documents: number;
  total_chunks: number;
  searchable_chunks: number;
  embedding_model: string;
  embedding_dimensions: number | null;
}

/** Mirrors app.api.integration_routes.IntegrationOut. */
export interface Integration {
  provider: string;
  is_enabled: boolean;
  connected: boolean;
  capabilities: string[];
  config: Record<string, unknown>;
  field_mappings: Record<string, string>;
  subscribed_events: string[];
  share_transcripts: boolean;
  connected_at: string | null;
  last_health_check_at: string | null;
  last_health_ok: boolean | null;
  last_error: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/** Mirrors app.api.integration_routes.IntegrationListOut. */
export interface IntegrationListResponse {
  integrations: Integration[];
}

/** Mirrors app.api.integration_routes.ProviderInfo. */
export interface ProviderInfo {
  provider: string;
  capabilities: string[];
  credential_fields: string[];
  config_fields: string[];
}

/** Mirrors app.api.integration_routes.ProviderCatalogueOut. */
export interface ProviderCatalogueResponse {
  providers: ProviderInfo[];
}

/** Mirrors app.api.integration_routes.SyncOut. */
export interface SyncItem {
  provider: string;
  entity_type: string;
  event_type: string | null;
  status: string;
  external_id: string | null;
  attempt_count: number;
  last_attempt_at: string | null;
  next_attempt_at: string | null;
  synced_at: string | null;
  last_error: string | null;
  last_error_code: string | null;
}

/** Mirrors app.api.integration_routes.SyncListOut. */
export interface SyncListResponse {
  syncs: SyncItem[];
  total: number;
}

/** Mirrors app.api.billing_routes.PlanOut. */
export interface Plan {
  code: string;
  name: string;
  description: string;
  currency: string;
  monthly_price_cents: number;
  annual_price_cents: number | null;
  included_voice_minutes: number;
  included_sms_segments: number;
  overage_voice_minute_cents: number;
  overage_enabled: boolean;
}

/** Mirrors app.api.billing_routes.UsageMetricOut. */
export interface UsageMetric {
  metric: string;
  unit: string;
  included: number;
  used: number;
  remaining: number;
  overage: number;
  percent_used: number;
  overage_cents: number;
}

/** Mirrors app.api.billing_routes.UsageOut. */
export interface UsageResponse {
  billing_period: string;
  period_start: string;
  period_end: string;
  plan_code: string;
  metrics: UsageMetric[];
  estimated_overage_cents: number;
  currency: string;
}

/** Mirrors app.api.billing_routes.BillingStatusOut. */
export interface BillingStatus {
  plan_code: string;
  plan_name: string;
  subscription_status: string;
  interval: string;
  provider: string;
  current_period_start: string | null;
  current_period_end: string | null;
  trial_end: string | null;
  cancel_at_period_end: boolean;
  pending_plan_code: string | null;
  last_invoice_status: string | null;
  estimated_overage_cents: number;
  currency: string;
  usage: Record<string, unknown>;
}

/** Mirrors app.api.billing_routes.InvoiceOut. */
export interface Invoice {
  id: string;
  status: string;
  currency: string;
  amount_due_cents: number;
  amount_paid_cents: number;
  period_start: string | null;
  period_end: string | null;
  hosted_invoice_url: string | null;
}

/** Mirrors app.api.appointment_routes.AppointmentOut. */
export interface Appointment {
  id: string;
  status: string;
  customer_name: string;
  customer_phone: string;
  attendee_email: string | null;
  reason: string;
  starts_at: string;
  ends_at: string;
  timezone: string;
  provider: string | null;
  external_event_id: string | null;
  meeting_url: string | null;
  cancelled_at: string | null;
  cancellation_reason: string | null;
  cancelled_by: string | null;
  rescheduled_from: string | null;
  confirmed_at: string | null;
}

/** Mirrors app.api.appointment_routes.AppointmentListOut. */
export interface AppointmentListResponse {
  appointments: Appointment[];
  total: number;
}

/** Mirrors app.api.appointment_routes.SlotOut. */
export interface Slot {
  start: string;
  end: string;
  spoken: string;
}

/** Mirrors app.api.appointment_routes.AvailabilityOut. */
export interface AvailabilityResponse {
  date: string;
  timezone: string;
  slots: Slot[];
  degraded: string | null;
}

/** Mirrors app.api.campaign_routes.CampaignOut. */
export interface Campaign {
  id: string;
  tenant_id: string;
  name: string;
  goal: string;
  channel: string;
  state: string;
  script_prompt: string;
  opening_line: string;
  lead_ids: string[];
  segment_ids: string[];
  daily_start: string;
  daily_end: string;
  days_of_week: number[];
  timezone: string;
  calls_per_minute: number;
  daily_limit: number;
  max_attempts_per_lead: number;
}

/** Mirrors app.api.campaign_routes.ResultsOut. */
export interface CampaignResults {
  metrics: Record<string, number>;
  eligibility: Record<string, number>;
}

/** Mirrors app.api.routes list_leads row. */
export interface Lead {
  id: string;
  name: string;
  phone: string;
  email: string | null;
  company: string | null;
  status: string;
  score: number | null;
  attempts: number;
  next_attempt_at: string | null;
}

/** Mirrors app.api.team_routes list_audit row. */
export interface AuditEntry {
  id: string;
  action: string;
  actor_email: string;
  target_user_id: string | null;
  ip_address: string;
  detail: Record<string, unknown>;
  created_at: string | null;
}

/** Mirrors app.api.routes.AgentConfigOut (credential-free projection). */
export interface AgentConfig {
  id: string;
  name: string;
  industry: string;
  twilio_number: string;
  agent_name: string;
  greeting: string;
  system_prompt_extra: string;
  llm_preset: string | null;
  llm_provider: string | null;
  llm_model: string | null;
  temperature: number;
  voice_id: string | null;
  language: string;
  humanize: boolean;
  vad_stop_secs: number;
  speech_speed: number;
  timezone: string;
  business_open: string;
  business_close: string;
  appointment_minutes: number;
  escalation_number: string | null;
  notify_sms_number: string | null;
  record_calls: boolean;
  recording_disclaimer: string;
  sms_enabled: boolean;
  whatsapp_enabled: boolean;
  ivr_enabled: boolean;
}

/** Mirrors GET /api/llm/presets row. */
export interface LlmPreset {
  key: string;
  brand: string;
  provider: string;
  model: string;
  latency_ms: number;
  notes: string;
}
