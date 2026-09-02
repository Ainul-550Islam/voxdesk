"""Central configuration. Everything comes from environment variables."""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Database
    database_url: str = "postgresql+asyncpg://voxdesk:voxdesk@localhost:5432/voxdesk"

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

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

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() in {"production", "prod"}

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

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

        return problems

    @property
    def ws_base_url(self) -> str:
        """Twilio needs wss:// for Media Streams."""
        return self.public_base_url.replace("https://", "wss://").replace("http://", "ws://")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

