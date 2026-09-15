"""Central configuration. Everything comes from environment variables."""
import json
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

#: The deployment environments the code understands. Anything else is
#: rejected at startup — fail closed rather than guessing.
KNOWN_APP_ENVS = frozenset({"development", "test", "staging", "production", "prod"})

#: Substrings that mark a value as an obvious placeholder. Used to refuse
#: obviously-unset secrets in production (a value that contains any of these
#: cannot be a real credential).
_PLACEHOLDER_MARKERS = (
    "change-me",
    "change_me",
    "insecure",
    "xxxx",
    "placeholder",
    "your-",
    "<",
    ">",
)


def _looks_placeholder(value: str | None) -> bool:
    """True when ``value`` is clearly a placeholder, not a real secret."""
    lowered = (value or "").lower()
    return bool(lowered) and any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # App
    app_env: str = "development"
    public_base_url: str = "http://localhost:8000"
    secret_key: str = "change-me"

    # ---- Authentication ----
    # MUST be overridden in production; validate_security() refuses to boot
    # with the placeholder. Generate with: openssl rand -hex 32
    jwt_secret: str = "insecure-development-only-change-me"
    jwt_issuer: str = "voxdesk"
    jwt_audience: str = "voxdesk-api"
    access_token_minutes: int = 15          # short-lived by design
    refresh_token_days: int = 14
    max_failed_logins: int = 8              # then a temporary lockout
    lockout_minutes: int = 15

    # Comma-separated browser origins allowed to call the API.
    cors_origins: str = "http://localhost:5173"

    # Comma-separated hostnames the HTTP layer will accept in the Host header
    # (TrustedHostMiddleware). Empty = the middleware is not installed, which
    # is the right default for single-proxy deployments where Caddy already
    # terminates TLS for exactly the configured domains. Set it in production
    # (e.g. "app.example.com") to reject Host-header spoofing and
    # host-header-based SSRF/cache-poisoning at the application layer too.
    trusted_hosts: str = ""

    # Database
    database_url: str = "postgresql+asyncpg://voxdesk:voxdesk@localhost:5432/voxdesk"

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # Twilio webhook signature verification is ON by default (fail-closed).
    # This opt-in flag disables it for local development so webhooks can be
    # exercised with curl and no valid X-Twilio-Signature. It MUST stay false
    # in production -- validate_security() refuses to boot otherwise.
    twilio_skip_webhook_verify: bool = False

    # ---- Real-call E2E (Step 5, scale-compliance) ----
    # The only way a *real* telephone call may exercise the AI voice pipeline
    # outside normal production traffic is a genuine, human-controlled test:
    # an operator dials a dedicated test number from an allowlisted phone, and
    # the dialed number maps to a tenant marked `is_test_tenant`. `e2e_enabled`
    # arms the guard; nothing here ever places a call. Production is rejected
    # outright (validate_security refuses to boot), and when armed, any call
    # that is not an explicit, allowlisted test call is refused at the door.
    e2e_enabled: bool = False
    # The dedicated Twilio number that the test tenant answers. Required when
    # e2e_enabled is set.
    e2e_test_number: str = ""
    # Comma-separated allowlist of operator caller numbers (E.164) permitted to
    # trigger a live E2E call. Empty means "nobody".
    e2e_allowed_callers: str = ""

    # Media-stream handshake: how long we wait for Twilio's "connected" +
    # "start" frames after accepting the socket before closing it. A client
    # that connects and never speaks would otherwise hold a database session
    # and a socket open forever (a stuck call). 0 disables the bound.
    stream_handshake_timeout_seconds: float = 15.0

    # Deepgram (STT)
    deepgram_api_key: str = ""
    deepgram_model: str = "nova-3"

    # ---- LLM providers (তিনটাই সাপোর্টেড, যেকোনো একটা থাকলেই চলবে) ----
    openai_api_key: str = ""          # ChatGPT
    anthropic_api_key: str = ""       # Claude
    google_api_key: str = ""          # Gemini
    default_llm_preset: str = "natural"   # fast | natural | cheap | smart

    # ElevenLabs (TTS)
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    elevenlabs_model: str = "eleven_flash_v2_5"

    # Channels
    whatsapp_enabled: bool = False
    twilio_whatsapp_number: str = ""      # e.g. whatsapp:+14155238886 (sandbox)

    # Default language for new tenants
    default_language: str = "en-US"

    # ---------- Knowledge base / RAG ----------
    # Storage. "local" writes under knowledge_local_path; "s3" uses the bucket.
    knowledge_storage_backend: str = "local"
    knowledge_local_path: str = "./var/knowledge"
    knowledge_s3_bucket: str = ""
    knowledge_s3_region: str = ""
    knowledge_s3_endpoint_url: str = ""      # for MinIO / R2 / Spaces

    # Upload limits. Enforced before any expensive processing.
    knowledge_max_file_mb: int = 20
    knowledge_max_documents_per_tenant: int = 2000

    # Chunking. Sizes are in characters; ~4 chars per token for English, so
    # 3200 chars is roughly 800 tokens -- comfortably inside every provider's
    # context while leaving room for the conversation itself.
    knowledge_chunk_chars: int = 3200
    knowledge_chunk_overlap_chars: int = 400
    knowledge_min_chunk_chars: int = 120

    # Embeddings. "hashing" is deterministic, offline and free -- correct for
    # local development and tests. Production must configure a real provider;
    # validate_security() refuses to boot otherwise.
    knowledge_embedding_provider: str = "hashing"
    knowledge_embedding_model: str = "hashing-v1"
    # 4096 is the right default *for the hashing embedder specifically*.
    # Measured on the evaluation corpus: at 512 dimensions, hash collisions
    # gave unrelated questions ("what is your return policy on tractors")
    # higher similarity than genuinely relevant chunks. At 4096 the noise
    # floor is exactly 0.0 and relevant chunks score 0.08-0.47, which is the
    # separation retrieval needs.
    #
    # SWITCHING PROVIDER? Set this to the model's real width -- 1536 for
    # text-embedding-3-small, 3072 for -large. Leaving it at 4096 will fail
    # at startup rather than silently mis-index.
    knowledge_embedding_dimensions: int = 4096
    knowledge_embedding_batch_size: int = 32
    knowledge_embedding_timeout_seconds: float = 20.0

    # Ingestion. "inline" runs indexing in a FastAPI background task, which
    # is right for development and small deployments; "worker" leaves the
    # document UPLOADED for scripts/scheduler.py to pick up.
    knowledge_ingest_mode: str = "inline"
    #: A document PROCESSING longer than this is assumed to belong to a dead
    #: worker and is reset to FAILED so it can be retried.
    knowledge_processing_timeout_seconds: int = 900

    # Retrieval.
    knowledge_top_k: int = 4
    # Similarity floor, and it MUST be retuned when the provider changes.
    #
    # The default hashing embedder is lexical: a chunk sharing two of a
    # question's four words scores around 0.05-0.20, while a chunk sharing
    # none scores exactly 0.0. The signal is clean but the scale is
    # compressed, so the floor's only job here is to exclude zero-overlap
    # noise. A real semantic provider has the opposite shape -- unrelated
    # text still scores 0.1-0.3 -- and needs roughly 0.30. Shipping the
    # lexical default against OpenAI embeddings would return everything.
    # See docs/KNOWLEDGE-RAG.md.
    knowledge_min_score: float = 0.03
    knowledge_rerank_enabled: bool = True
    knowledge_rerank_candidates: int = 12
    #: Hard ceiling for retrieval during a live call. A caller will not wait.
    knowledge_retrieval_timeout_seconds: float = 1.5
    knowledge_context_max_chars: int = 4000

    # Google
    google_credentials_json: str = "./secrets/google_service_account.json"

    # ---------- Observability (STEP 9) ----------
    # Sentry error reporting. Optional: when empty nothing is initialised and
    # unhandled exceptions are reported nowhere (fine for development). Set it
    # to your project DSN in production. Never logged, never returned by an API.
    sentry_dsn: str = ""

    # Log level (DEBUG|INFO|WARNING|ERROR) and renderer. Production defaults to
    # machine-readable JSON (see app/core/logging.py); development keeps the
    # coloured console output. LOG_FORMAT accepts "console" or "json".
    log_level: str = "INFO"
    log_format: str = "console"

    # ---------- Data policy / compliance (STEP 9) ----------
    # Retention for call recordings and transcripts, which hold personal data.
    # Operator-configurable per jurisdiction: GDPR/EU and healthcare buyers
    # usually want 30-90 days; 365 is a safe default for the US. Records older
    # than the cutoff are flagged for deletion (app/core/data_policy.py,
    # docs/COMPLIANCE.md).
    call_retention_days: int = 365

    # EU AI Act transparency (Art. 50) and several US state laws: a caller must
    # know they are speaking to an automated agent. Default true; the tenant's
    # greeting is checked with app.core.data_policy.ai_disclosure_present().
    ai_disclosure_required: bool = True

    # ---------- Rate limiting (STEP 9) ----------
    # OFF by default so tests and local dev are unaffected; production sets
    # RATE_LIMIT_ENABLED=true. The limiter fails closed (rejects) on error.
    rate_limit_enabled: bool = False
    rate_limit_burst: int = 300        # non-auth requests per minute per IP
    rate_limit_login_per_minute: int = 10   # auth requests per minute per IP

    # ---------- Redis / cache (STEP 9) ----------
    # Empty = in-process cache and rate limiting (single worker). Set
    # REDIS_URL=redis://redis:6379/0 in multi-worker production.
    redis_url: str = ""
    cache_ttl_seconds: int = 60

    # ---------- Database pool (STEP 9) ----------
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # ---------- Metrics (STEP 9) ----------
    # Prometheus /metrics endpoint. Disabled by default; production sets
    # METRICS_ENABLED=true and a METRICS_TOKEN for the scraper.
    metrics_enabled: bool = False
    metrics_token: str = ""

    # ---------- Cost awareness (STEP 7 scale-compliance) ----------
    # Operator-provided *provider* unit prices, in millicents per smallest
    # unit, as a JSON object. Keys are a fixed vocabulary; anything unknown is
    # ignored. A missing or zero price means UNKNOWN — the cost metric is
    # reported with cost_known=0 rather than an invented number. See
    # docs/COST-AWARENESS.md.
    #
    #   {"voice_minute": 1300, "sms_segment": 790,
    #    "llm_1k_tokens:openai": 15, "llm_1k_tokens:anthropic": 80,
    #    "tts_1k_chars": 30}
    #
    # These are what VoxDesk *pays providers*, not what tenants are charged
    # (the plan catalogue owns revenue). Never a billing authority.
    cost_unit_prices_json: str = ""

    # ---------- Failure injection (STEP 7 scale-compliance) ----------
    # Deterministic chaos for load tests and SLO drills. OFF by default and
    # hard-refused in production (validate_security). Rules are exact-path
    # matches with a fixed effect — never probabilistic. See
    # docs/FAILURE-INJECTION.md.
    chaos_enabled: bool = False
    chaos_rules_json: str = ""

    # ---------- Scheduler metrics (STEP 7 scale-compliance) ----------
    # The background worker is a separate process, so its job metrics need a
    # scrape target of their own. prometheus_client.start_http_server binds
    # this port on the scheduler container (compose-network only). 0 disables.
    scheduler_metrics_port: int = 8001

    # ---------- security.txt (RFC 9116, STEP 9) ----------
    # Contact for security researchers and buyers' security teams. Empty = the
    # /.well-known/security.txt endpoint returns 404 (a contact-less file is
    # worse than none). Set it to a monitored email or https:// URL in
    # production.
    security_contact: str = ""

    # ---------- Licensing (STEP 9) ----------
    # HMAC key for self-hosted/white-label license tokens. Falls back to
    # JWT_SECRET when empty (acceptable for a single deployment; set a distinct
    # key when reselling licenses across deployments).
    license_secret: str = ""

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @property
    def is_staging(self) -> bool:
        """True for the dedicated pre-production environment.

        Staging is deliberately NOT production: it may run the real-call E2E
        guard, may enable failure injection, and does not hard-fail readiness
        on missing provider keys. It is also NOT development: schema is owned
        by Alembic (no ``create_all``), and its secrets must be its own.
        """
        return self.app_env.lower() == "staging"

    @property
    def uses_https(self) -> bool:
        """Whether the deployment is served over TLS.

        Drives the Secure cookie flag and the HSTS header from the actual URL
        scheme rather than a guess about the environment: a staging box that
        is TLS-terminated gets Secure cookies and HSTS exactly like
        production.
        """
        return self.public_base_url.startswith("https://")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def trusted_host_list(self) -> list[str]:
        """Hostnames allowed in the Host header. Empty list = middleware off."""
        return [h.strip().lower() for h in self.trusted_hosts.split(",") if h.strip()]

    @property
    def e2e_caller_list(self) -> list[str]:
        return [n.strip() for n in self.e2e_allowed_callers.split(",") if n.strip()]

    @property
    def cost_unit_prices(self) -> dict[str, int]:
        """The parsed operator price table, or ``{}`` when unset/malformed.

        Malformed JSON is not an exception here — startup validation reports it
        and the cost layer treats every unknown/missing price as UNKNOWN.
        """
        raw = (self.cost_unit_prices_json or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(k): int(v) for k, v in parsed.items() if isinstance(v, (int, float))}

    @property
    def chaos_rules(self) -> list[dict]:
        """The parsed chaos rule list, or ``[]`` when unset/malformed."""
        raw = (self.chaos_rules_json or "").strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        return [r for r in parsed if isinstance(r, dict)]

    # ---------- CRM integrations (STEP 5) ----------
    # Application-level encryption of provider credentials at rest.
    # Format: "key_id:base64key,older_id:base64key" -- first entry is active.
    # Generate: python -c "import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
    # Empty is allowed in development (integrations simply cannot store
    # credentials); validate_security() refuses to boot production without it.
    crm_encryption_keys: str = ""

    # Per-request ceiling for any provider call. Deliberately short: this runs
    # on a worker, but a provider hanging for 60s still blocks the pass.
    crm_request_timeout_seconds: float = 10.0

    # Retry policy (requirement 13: must be configurable).
    crm_retry_max_attempts: int = 5
    crm_retry_base_seconds: float = 2.0
    crm_retry_max_seconds: float = 900.0

    # VoxDesk-side per-(tenant, provider) protection, so one broken tenant
    # cannot consume the shared worker.
    crm_rate_limit_per_second: float = 5.0
    crm_rate_limit_burst: float = 20.0

    # Worker loop.
    crm_sync_interval_seconds: int = 20
    crm_sync_batch_size: int = 20
    crm_stuck_sync_minutes: int = 15

    # Step 6 (scale-compliance): how long a reminder-send lease is valid. A
    # worker claims a reminder, sends the SMS, and clears the lease; if it
    # dies mid-send the reaper reclaims the row after this many seconds. Long
    # enough to cover a slow Twilio call, short enough that a dead worker
    # cannot block a customer's reminder indefinitely.
    reminder_lease_seconds: int = 300

    # Inbound provider webhooks: how much clock skew to tolerate before an
    # event is treated as a replay.
    crm_webhook_tolerance_seconds: int = 300

    # ---------- Calendar / scheduling (STEP 6) ----------
    # Per-request ceiling for any calendar provider call. Shorter than the CRM
    # one because this runs while a caller is on the phone: an availability
    # lookup that takes eight seconds has already lost the conversation.
    calendar_request_timeout_seconds: float = 6.0

    # The hard bound the voice agent applies on top. Requirement 30: an
    # availability lookup must not hang indefinitely.
    calendar_voice_timeout_seconds: float = 3.0

    # Inbound provider notifications: clock skew tolerated before an event is
    # treated as a replay.
    calendar_webhook_tolerance_seconds: int = 300

    # Refresh an OAuth access token this long before it actually expires, so a
    # booking never races the expiry.
    calendar_token_refresh_margin_seconds: int = 300

    # ---------- Billing (STEP 7) ----------
    # "stripe" or "manual". Manual is a real mode, not a stub: plans,
    # entitlements, metering and periods all work; only the payment rail is
    # absent. It is the correct setting for development and for
    # invoice-me contracts.
    billing_provider: str = "manual"

    # Stripe secrets. Never returned by an API, never logged, never in a JWT.
    # validate_security() refuses to boot production with provider=stripe and
    # either of these empty.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_publishable_key: str = ""      # safe for a browser; not a secret

    # Where Stripe returns the browser. Required only when checkout is used.
    billing_checkout_success_url: str = ""
    billing_checkout_cancel_url: str = ""
    billing_portal_return_url: str = ""

    # Per-request ceiling for a provider call. Generous relative to the voice
    # path because nothing here runs while a caller is on the line.
    billing_request_timeout_seconds: float = 15.0

    # Requirement 25's "clearly defined unlimited entitlement". An explicit
    # switch rather than an environment guess -- an implicit staging default
    # is how unlimited reaches production.
    billing_unlimited_entitlements: bool = False

    # Requirement 26. Inbound calls are never blocked (see the audit's F10:
    # hanging up on a dentist's patients because the dentist owes money is a
    # product decision nobody made deliberately). This gates *outbound* and
    # account-level actions only.
    billing_enforce_entitlements: bool = True

    def validate_security(self) -> list[str]:
        """
        Fatal misconfigurations. Called at startup; in production the app
        refuses to boot rather than serving traffic with a known-bad secret.
        """
        problems: list[str] = []

        # ---- Deployment environment (STEP 8) ----
        if self.app_env.lower() not in KNOWN_APP_ENVS:
            problems.append(
                f"APP_ENV must be one of {sorted(KNOWN_APP_ENVS)}, got "
                f"{self.app_env!r}"
            )

        if self.is_production:
            if self.rate_limit_enabled is not True:
                problems.append(
                    "RATE_LIMIT_ENABLED must be true in production; the "
                    "limiter fails closed and must not be silently off"
                )
            if (self.log_level or "").upper() == "DEBUG":
                problems.append(
                    "LOG_LEVEL=DEBUG is not allowed in production; verbose "
                    "logs can leak caller data"
                )
            if "localhost" in self.public_base_url or "127.0.0.1" in self.public_base_url:
                problems.append(
                    "PUBLIC_BASE_URL must be a public https URL in production, "
                    "not localhost"
                )
            for origin in self.cors_origin_list:
                if not origin.startswith("https://"):
                    problems.append(
                        f"CORS_ORIGINS entry {origin!r} must use https in "
                        "production; development origins cannot reach prod"
                    )
            # Placeholder secrets: an obviously-unset credential must not
            # boot a production deployment that would then fail on live calls.
            for field, value in (
                ("SECRET_KEY", self.secret_key),
                ("JWT_SECRET", self.jwt_secret),
                ("TWILIO_AUTH_TOKEN", self.twilio_auth_token),
                ("DEEPGRAM_API_KEY", self.deepgram_api_key),
                ("ELEVENLABS_API_KEY", self.elevenlabs_api_key),
                ("OPENAI_API_KEY", self.openai_api_key),
                ("ANTHROPIC_API_KEY", self.anthropic_api_key),
                ("GOOGLE_API_KEY", self.google_api_key),
                ("STRIPE_SECRET_KEY", self.stripe_secret_key),
                ("STRIPE_WEBHOOK_SECRET", self.stripe_webhook_secret),
            ):
                if value and _looks_placeholder(value):
                    problems.append(
                        f"{field} looks like a placeholder and must be "
                        "replaced in production"
                    )

        if self.jwt_secret == Settings.model_fields["jwt_secret"].default:
            problems.append("JWT_SECRET is still the built-in default")
        if len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET must be at least 32 characters")
        if self.secret_key in {"", "change-me"}:
            problems.append("SECRET_KEY is still the built-in default")
        if "*" in self.cors_origin_list:
            problems.append("CORS_ORIGINS must not be '*' when credentials are allowed")
        if self.is_production and not self.public_base_url.startswith("https://"):
            problems.append("PUBLIC_BASE_URL must use https in production")
        if self.is_production and not self.twilio_auth_token:
            problems.append("TWILIO_AUTH_TOKEN is required to verify webhooks")
        if self.is_production and self.twilio_skip_webhook_verify:
            problems.append(
                "TWILIO_SKIP_WEBHOOK_VERIFY must not be enabled in production; "
                "it disables Twilio webhook signature verification"
            )

        if self.e2e_enabled:
            if self.is_production:
                problems.append(
                    "E2E_ENABLED must not be set in production; real-call E2E "
                    "is operator-run in a test environment only"
                )
            if not self.e2e_test_number:
                problems.append(
                    "E2E_TEST_NUMBER is required when E2E_ENABLED is set"
                )
            if not self.e2e_caller_list:
                problems.append(
                    "E2E_ALLOWED_CALLERS is required when E2E_ENABLED is set"
                )

        if self.knowledge_storage_backend == "s3" and not self.knowledge_s3_bucket:
            problems.append("KNOWLEDGE_S3_BUCKET is required when the backend is s3")
        if self.is_production and self.knowledge_embedding_provider == "hashing":
            problems.append(
                "KNOWLEDGE_EMBEDDING_PROVIDER=hashing is a development stub; "
                "configure a real embedding provider in production"
            )

        # ---- CRM credentials (STEP 5) ----
        # Storing a provider token with no encryption key configured would put
        # plaintext CRM credentials in the database, so production refuses.
        if self.is_production and not self.crm_encryption_keys:
            problems.append(
                "CRM_ENCRYPTION_KEYS is required in production; CRM provider "
                "credentials must not be stored without application-level "
                "encryption"
            )
        if self.crm_encryption_keys:
            # Fail at boot on a malformed key rather than at the moment a
            # tenant first tries to connect an integration.
            try:
                from app.integrations.crm.crypto import parse_key_ring

                parse_key_ring(self.crm_encryption_keys)
            except Exception as exc:
                problems.append(f"CRM_ENCRYPTION_KEYS is invalid: {exc}")
        if self.crm_retry_max_attempts < 1:
            problems.append("CRM_RETRY_MAX_ATTEMPTS must be at least 1")

        # ---- Billing (STEP 7) ----
        # Requirement 6: do not let development defaults run in production.
        if self.billing_provider not in ("stripe", "manual"):
            problems.append(
                f"BILLING_PROVIDER must be 'stripe' or 'manual', got "
                f"{self.billing_provider!r}"
            )
        if self.billing_provider == "stripe":
            if not self.stripe_secret_key:
                problems.append(
                    "STRIPE_SECRET_KEY is required when BILLING_PROVIDER=stripe"
                )
            if not self.stripe_webhook_secret:
                problems.append(
                    "STRIPE_WEBHOOK_SECRET is required when BILLING_PROVIDER=stripe; "
                    "without it the webhook endpoint cannot verify signatures and "
                    "anyone could forge a subscription"
                )
            if self.is_production and self.stripe_secret_key.startswith("sk_test_"):
                problems.append(
                    "STRIPE_SECRET_KEY is a test-mode key; production would take "
                    "no real payments"
                )
        if self.is_production and self.billing_unlimited_entitlements:
            problems.append(
                "BILLING_UNLIMITED_ENTITLEMENTS must not be enabled in production; "
                "every plan limit would be ignored"
            )

        # ---- Cost awareness (STEP 7 scale-compliance) ----
        if self.cost_unit_prices_json.strip():
            try:
                parsed = json.loads(self.cost_unit_prices_json)
            except json.JSONDecodeError as exc:
                problems.append(f"COST_UNIT_PRICES is not valid JSON: {exc}")
            else:
                if not isinstance(parsed, dict):
                    problems.append("COST_UNIT_PRICES must be a JSON object")
                else:
                    for key, value in parsed.items():
                        if not isinstance(value, (int, float)) or value < 0:
                            problems.append(
                                f"COST_UNIT_PRICES[{key!r}] must be a non-negative number"
                            )

        # ---- Failure injection (STEP 7 scale-compliance) ----
        if self.chaos_enabled:
            if self.is_production:
                problems.append(
                    "CHAOS_ENABLED must not be set in production; failure "
                    "injection is for load tests and SLO drills only"
                )
            if not self.chaos_rules:
                problems.append(
                    "CHAOS_RULES is required when CHAOS_ENABLED is set"
                )
        elif self.chaos_rules_json.strip():
            # Rules configured but the switch off: harmless, but worth saying
            # so an operator who expects chaos to be live notices it is not.
            try:
                json.loads(self.chaos_rules_json)
            except json.JSONDecodeError as exc:
                problems.append(f"CHAOS_RULES is not valid JSON: {exc}")

        return problems

    @property
    def ws_base_url(self) -> str:
        """Twilio needs wss:// for Media Streams."""
        return self.public_base_url.replace("https://", "wss://").replace("http://", "ws://")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()