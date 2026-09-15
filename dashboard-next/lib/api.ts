// API client for the VoxDesk FastAPI backend.
//
// * Every request carries the access token as a Bearer header and sends
//   credentials so the HttpOnly refresh cookie flows with it.
// * A 401 triggers a single refresh-and-retry (POST /auth/refresh), then a
//   redirect to /login only if the refresh also fails.
// * Error bodies use FastAPI's `detail` shape, both the plain string form and
//   the 422 validation-list form.

import { clearToken, getToken, setToken } from "./auth";
import type {
  AgentConfig,
  AppointmentListResponse,
  AuditEntry,
  AvailabilityResponse,
  BillingStatus,
  CallDetail,
  CallListResponse,
  Campaign,
  CampaignResults,
  DocumentListResponse,
  IntegrationListResponse,
  Invoice,
  KnowledgeStats,
  Lead,
  LlmPreset,
  MeResponse,
  OverviewResponse,
  Plan,
  ProviderCatalogueResponse,
  SearchResponse,
  SyncListResponse,
  TokenResponse,
  TranscriptTurn,
  UsageResponse,
  User,
} from "./types";

const RAW_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
export const BASE_URL = RAW_BASE_URL.replace(/\/+$/, "");

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function detailFromBody(body: unknown): string {
  if (body && typeof body === "object") {
    const record = body as Record<string, unknown>;
    if (typeof record.detail === "string") return record.detail;
    if (Array.isArray(record.detail)) {
      const first = record.detail[0] as { msg?: unknown } | undefined;
      if (first && typeof first.msg === "string") return first.msg;
    }
  }
  return "request failed";
}

let refreshInFlight: Promise<boolean> | null = null;

async function tryRefresh(): Promise<boolean> {
  if (typeof window === "undefined") return false;
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const res = await fetch(`${BASE_URL}/auth/refresh`, {
          method: "POST",
          credentials: "include",
          headers: { Accept: "application/json" },
        });
        if (!res.ok) return false;
        const body = (await res.json()) as TokenResponse;
        setToken(body.access_token);
        return true;
      } catch {
        return false;
      } finally {
        refreshInFlight = null;
      }
    })();
  }
  return refreshInFlight;
}

interface RequestOptions {
  /** When false, a 401 is reported as-is instead of attempting a refresh. */
  retryOn401?: boolean;
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  options: RequestOptions = {},
): Promise<T> {
  const { retryOn401 = true } = options;
  const token = getToken();

  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers,
    credentials: "include",
  });

  if (res.status === 401 && retryOn401) {
    const refreshed = await tryRefresh();
    if (refreshed) {
      return request<T>(path, init, { retryOn401: false });
    }
    clearToken();
    if (typeof window !== "undefined" && window.location.pathname !== "/login") {
      window.location.assign("/login");
    }
    throw new ApiError(401, "session expired");
  }

  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      detail = detailFromBody(await res.json());
    } catch {
      // Non-JSON error body; keep the status-based message.
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  // ---- auth ----
  login(email: string, password: string): Promise<TokenResponse> {
    return request<TokenResponse>(
      "/auth/login",
      { method: "POST", body: JSON.stringify({ email, password }) },
      { retryOn401: false },
    );
  },

  me(): Promise<MeResponse> {
    return request<MeResponse>("/auth/me");
  },

  // ---- overview / analytics ----
  overview(): Promise<OverviewResponse> {
    return request<OverviewResponse>("/api/analytics/overview");
  },

  // ---- calls ----
  listCalls(
    tenantId: string,
    params: {
      limit?: number;
      offset?: number;
      status?: string;
      direction?: string;
      booked?: boolean;
      transferred?: boolean;
      search?: string;
    } = {},
  ): Promise<CallListResponse> {
    const qs = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== "") qs.set(key, String(value));
    }
    const query = qs.toString();
    return request<CallListResponse>(
      `/api/tenants/${tenantId}/calls${query ? `?${query}` : ""}`,
    );
  },

  callDetail(callId: string): Promise<CallDetail> {
    return request<CallDetail>(`/api/calls/${callId}`);
  },

  transcript(callId: string): Promise<TranscriptTurn[]> {
    return request<TranscriptTurn[]>(`/api/calls/${callId}/transcript`);
  },

  // ---- knowledge ----
  knowledgeDocuments(): Promise<DocumentListResponse> {
    return request<DocumentListResponse>("/api/knowledge/documents");
  },

  knowledgeStats(): Promise<KnowledgeStats> {
    return request<KnowledgeStats>("/api/knowledge/stats");
  },

  knowledgeSearch(query: string, topK?: number): Promise<SearchResponse> {
    return request<SearchResponse>(
      "/api/knowledge/search",
      {
        method: "POST",
        body: JSON.stringify({ query, top_k: topK ?? null }),
      },
      { retryOn401: true },
    );
  },

  // ---- integrations (CRM) ----
  crmIntegrations(): Promise<IntegrationListResponse> {
    return request<IntegrationListResponse>("/api/integrations/crm");
  },

  crmProviders(): Promise<ProviderCatalogueResponse> {
    return request<ProviderCatalogueResponse>("/api/integrations/crm/providers");
  },

  crmSyncs(): Promise<SyncListResponse> {
    return request<SyncListResponse>("/api/integrations/crm/syncs");
  },

  crmDisconnect(provider: string): Promise<void> {
    return request<void>(`/api/integrations/crm/${provider}/disconnect`, {
      method: "POST",
    });
  },

  // ---- team + audit ----
  teamUsers(): Promise<User[]> {
    return request<User[]>("/api/team/users");
  },

  teamAudit(): Promise<AuditEntry[]> {
    return request<AuditEntry[]>("/api/team/audit");
  },

  // ---- billing ----
  billingStatus(): Promise<BillingStatus> {
    return request<BillingStatus>("/api/billing");
  },

  billingPlans(): Promise<Plan[]> {
    return request<Plan[]>("/api/billing/plans");
  },

  billingUsage(): Promise<UsageResponse> {
    return request<UsageResponse>("/api/billing/usage");
  },

  billingInvoices(): Promise<Invoice[]> {
    return request<Invoice[]>("/api/billing/invoices");
  },

  // ---- appointments ----
  appointments(
    params: { status?: string; upcoming?: boolean } = {},
  ): Promise<AppointmentListResponse> {
    const qs = new URLSearchParams();
    if (params.status) qs.set("status", params.status);
    if (params.upcoming) qs.set("upcoming", "true");
    const query = qs.toString();
    return request<AppointmentListResponse>(
      `/api/appointments${query ? `?${query}` : ""}`,
    );
  },

  availability(day: string): Promise<AvailabilityResponse> {
    return request<AvailabilityResponse>(
      `/api/appointments/availability?day=${encodeURIComponent(day)}`,
    );
  },

  // ---- campaigns ----
  campaigns(): Promise<Campaign[]> {
    return request<Campaign[]>("/api/campaigns");
  },

  campaignResults(campaignId: string): Promise<CampaignResults> {
    return request<CampaignResults>(`/api/campaigns/${campaignId}/results`);
  },

  // ---- leads ----
  leads(tenantId: string): Promise<Lead[]> {
    return request<Lead[]>(`/api/tenants/${tenantId}/leads`);
  },

  // ---- agent ----
  agentConfig(tenantId: string): Promise<AgentConfig> {
    return request<AgentConfig>(`/api/tenants/${tenantId}/agent`);
  },

  llmPresets(): Promise<LlmPreset[]> {
    return request<LlmPreset[]>("/api/llm/presets");
  },
};
