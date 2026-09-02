"""Database schema.

Multi-tenant from day one: every business you sell to is a Tenant row.
This is what lets you charge $199/month x N clients from one deployment.
"""
from __future__ import annotations

import enum
import uuid
from datetime import datetime, time

from sqlalchemy import (
    Boolean, DateTime, Enum, Float, ForeignKey, Index, Integer, JSON, String, Text, Time, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class CallStatus(str, enum.Enum):
    """
    Lifecycle of a single call.

    NOTE: SQLAlchemy's Enum() persists the member *name* (e.g. "NO_ANSWER"),
    not the value. The PostgreSQL type `callstatus` must therefore contain
    exactly these six names -- see alembic/versions/0002_enum_consistency.py.
    """
    RINGING = "ringing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    NO_ANSWER = "no_answer"       # Twilio "no-answer": rang out, nobody picked up
    TRANSFERRED = "transferred"   # handed to a human via <Dial>


class TransferState(str, enum.Enum):
    """
    Human-escalation sub-state, tracked separately from CallStatus.

    A call can be IN_PROGRESS while a transfer is mid-flight, so this cannot be
    folded into CallStatus. It also doubles as the idempotency lock: moving out
    of NONE is what stops a second tool call from dialling the human twice.

    NOTE: SQLAlchemy's Enum() persists the member NAME, so the PostgreSQL type
    `transferstate` must contain NONE/REQUESTED/DIALING/CONNECTED/FAILED --
    see alembic/versions/0004_call_transfer_lifecycle.py.
    """
    NONE = "none"             # no escalation has been requested
    REQUESTED = "requested"   # the AI asked; we have not told the provider yet
    DIALING = "dialing"       # the provider accepted; the human's phone is ringing
    CONNECTED = "connected"   # the human answered
    FAILED = "failed"         # busy, no answer, rejected, or a provider error


#: States in which a transfer is already under way or finished. A second
#: request while in one of these must be a no-op, never a second phone call.
TRANSFER_IN_FLIGHT = frozenset({
    TransferState.REQUESTED,
    TransferState.DIALING,
    TransferState.CONNECTED,
})


class CallDirection(str, enum.Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class LeadStatus(str, enum.Enum):
    NEW = "new"
    QUEUED = "queued"
    CALLED = "called"
    QUALIFIED = "qualified"
    UNQUALIFIED = "unqualified"
    FAILED = "failed"
    DNC = "do_not_call"          # legally required: never call again


class UserRole(str, enum.Enum):
    """
    Ordered by privilege. `rbac.ROLE_LEVEL` turns these into integers so a
    role can never grant something above itself.
    """
    OWNER = "owner"
    ADMIN = "admin"
    MANAGER = "manager"
    AGENT = "agent"
    VIEWER = "viewer"


class AuditAction(str, enum.Enum):
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILURE = "login_failure"
    LOGOUT = "logout"
    TOKEN_REFRESH = "token_refresh"
    USER_CREATED = "user_created"
    USER_DEACTIVATED = "user_deactivated"
    USER_REACTIVATED = "user_reactivated"
    ROLE_CHANGED = "role_changed"
    PASSWORD_CHANGED = "password_changed"
    AUTHZ_DENIED = "authz_denied"
    # STEP 5. `detail` on these carries provider and outcome only -- never a
    # token, never a config value that could hold one.
    INTEGRATION_CONNECTED = "integration_connected"
    INTEGRATION_UPDATED = "integration_updated"
    INTEGRATION_DISCONNECTED = "integration_disconnected"
    INTEGRATION_TESTED = "integration_tested"
    # STEP 6. Same rule: provider and outcome only, never a token.
    CALENDAR_CONNECTED = "calendar_connected"
    CALENDAR_DISCONNECTED = "calendar_disconnected"
    APPOINTMENT_CANCELLED = "appointment_cancelled"
    APPOINTMENT_RESCHEDULED = "appointment_rescheduled"
    # STEP 7. `detail` carries plan codes and outcomes only -- never a card
    # number, never a Stripe key, never a webhook secret.
    BILLING_CHECKOUT_STARTED = "billing_checkout_started"
    BILLING_SUBSCRIPTION_CREATED = "billing_subscription_created"
    BILLING_SUBSCRIPTION_CHANGED = "billing_subscription_changed"
    BILLING_CANCELLATION_REQUESTED = "billing_cancellation_requested"
    BILLING_CANCELLATION_COMPLETED = "billing_cancellation_completed"
    BILLING_PAYMENT_FAILED = "billing_payment_failed"
    BILLING_PLAN_CHANGED = "billing_plan_changed"
    BILLING_LIMIT_HIT = "billing_limit_hit"
    BILLING_ADJUSTMENT = "billing_adjustment"


class Speaker(str, enum.Enum):
    """
    Who produced a transcript turn.

    Canonical values are USER / ASSISTANT / SYSTEM. They match the role
    vocabulary the LLM context and the text channels already use, and they
    match the `speaker` PostgreSQL type created by the baseline migration,
    so no persisted row has to be rewritten on an Alembic-managed database.
    """
    USER = "user"            # the caller / the person texting
    ASSISTANT = "assistant"  # the AI receptionist
    SYSTEM = "system"        # system notes (transfer, timeout, error)


class Tenant(Base):
    """One business = one tenant. Their phone number routes to their config."""
    __tablename__ = "tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200))
    industry: Mapped[str] = mapped_column(String(80), default="general")   # dental, legal, restaurant...
    twilio_number: Mapped[str] = mapped_column(String(32), unique=True, index=True)

    # Agent personality / knowledge
    agent_name: Mapped[str] = mapped_column(String(80), default="Alex")
    greeting: Mapped[str] = mapped_column(Text, default="Thanks for calling. How can I help you today?")
    system_prompt_extra: Mapped[str] = mapped_column(Text, default="")
    knowledge_base: Mapped[dict] = mapped_column(JSON, default=dict)  # {"hours": "...", "services": [...]}

    # ---- LLM choice (প্রতি ক্লায়েন্টে আলাদা AI) ----
    llm_preset: Mapped[str | None] = mapped_column(String(32), default="natural")
    llm_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)  # openai|anthropic|google
    llm_model: Mapped[str | None] = mapped_column(String(80), nullable=True)
    temperature: Mapped[float] = mapped_column(Float, default=0.65)

    # ---- মানুষের মতো শোনানোর সেটিং ----
    humanize: Mapped[bool] = mapped_column(Boolean, default=True)
    vad_stop_secs: Mapped[float] = mapped_column(Float, default=0.45)
    speech_speed: Mapped[float] = mapped_column(Float, default=1.0)
    voice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    language: Mapped[str] = mapped_column(String(16), default="en-US")

    # Behaviour
    timezone: Mapped[str] = mapped_column(String(64), default="America/New_York")
    business_open: Mapped[time] = mapped_column(Time, default=time(9, 0))
    business_close: Mapped[time] = mapped_column(Time, default=time(17, 0))
    appointment_minutes: Mapped[int] = mapped_column(Integer, default=30)
    escalation_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    notify_sms_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Integrations
    google_calendar_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    crm_webhook_url: Mapped[str | None] = mapped_column(String(500), nullable=True)   # GHL / Zapier / Make / n8n
    crm_type: Mapped[str] = mapped_column(String(32), default="webhook")              # webhook|gohighlevel|hubspot
    crm_api_key: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Outbound calling (cold calls, follow-ups, reminders)
    outbound_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    outbound_caller_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    outbound_window_open: Mapped[time] = mapped_column(Time, default=time(9, 0))      # TCPA: no calls before 8am
    outbound_window_close: Mapped[time] = mapped_column(Time, default=time(20, 0))    # TCPA: none after 9pm
    max_call_attempts: Mapped[int] = mapped_column(Integer, default=3)

    # Reminders
    reminder_hours_before: Mapped[int] = mapped_column(Integer, default=24)
    reminder_enabled: Mapped[bool] = mapped_column(Boolean, default=False)

    # IVR / call flow (JSON so the dashboard can edit it without a deploy)
    ivr_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ivr_flow: Mapped[dict] = mapped_column(JSON, default=dict)

    # Text channels
    sms_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    whatsapp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    whatsapp_number: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # A2P 10DLC (US SMS is silently filtered without this)
    a2p_brand_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    a2p_campaign_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    a2p_status: Mapped[str] = mapped_column(String(32), default="not_started")

    # Compliance / recording
    record_calls: Mapped[bool] = mapped_column(Boolean, default=False)
    recording_disclaimer: Mapped[str] = mapped_column(
        Text, default="This call may be recorded for quality purposes."
    )

    # Billing
    plan: Mapped[str] = mapped_column(String(32), default="starter")   # starter / pro
    included_minutes: Mapped[int] = mapped_column(Integer, default=500)
    minutes_used: Mapped[float] = mapped_column(Float, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    calls: Mapped[list["Call"]] = relationship(back_populates="tenant")


class Call(Base):
    __tablename__ = "calls"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    call_sid: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    from_number: Mapped[str] = mapped_column(String(32))
    to_number: Mapped[str] = mapped_column(String(32))
    status: Mapped[CallStatus] = mapped_column(Enum(CallStatus), default=CallStatus.RINGING)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)

    # Outcome
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    intent: Mapped[str | None] = mapped_column(String(80), nullable=True)
    booked: Mapped[bool] = mapped_column(Boolean, default=False)
    escalated: Mapped[bool] = mapped_column(Boolean, default=False)

    # Latency stats -- your product's #1 quality metric
    avg_response_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    llm_used: Mapped[str | None] = mapped_column(String(80), nullable=True)

    direction: Mapped[CallDirection] = mapped_column(
        Enum(CallDirection), default=CallDirection.INBOUND
    )
    recording_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    lead_score: Mapped[int | None] = mapped_column(Integer, nullable=True)   # 0-100

    # Why the call ended badly ("busy", "no-answer", ...). Only set for
    # FAILED/NO_ANSWER; `summary` stays the human-readable outcome.
    failure_reason: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # --- human transfer -------------------------------------------------
    # `escalated` (above) stays as the boolean the dashboard and CRM payload
    # already read; these columns record how the escalation actually went.
    transfer_state: Mapped[TransferState] = mapped_column(
        Enum(TransferState), default=TransferState.NONE, nullable=False
    )
    transfer_destination: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transfer_reason: Mapped[str | None] = mapped_column(String(400), nullable=True)
    transfer_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    transfer_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    transfer_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    transfer_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    transfer_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    transfer_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    crm_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id"), nullable=True, index=True
    )

    tenant: Mapped[Tenant] = relationship(back_populates="calls")
    turns: Mapped[list["Turn"]] = relationship(back_populates="call", cascade="all, delete-orphan")


class Turn(Base):
    """One utterance. Storing these gives you transcripts + training data."""
    __tablename__ = "turns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    call_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("calls.id"), index=True)
    speaker: Mapped[Speaker] = mapped_column(Enum(Speaker))
    text: Mapped[str] = mapped_column(Text)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    call: Mapped[Call] = relationship(back_populates="turns")


class AppointmentStatus(str, enum.Enum):
    """
    Lifecycle of one booking.

    Before STEP 6 there was no status at all: a row existed or it did not, so
    there was no way to cancel, to record a no-show, or -- most importantly --
    to distinguish "the provider accepted this" from "we wrote a row and hoped".

    PENDING    persisted, provider has not accepted (yet, or ever)
    CONFIRMED  the provider accepted and returned an event id
    RESCHEDULED  moved; `starts_at` is the new time
    CANCELLED  cancelled by anyone
    NO_SHOW    the customer did not arrive
    FAILED     the provider refused permanently; nothing is on the calendar
    """
    PENDING = "pending"
    CONFIRMED = "confirmed"
    RESCHEDULED = "rescheduled"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"
    FAILED = "failed"


#: Statuses that still occupy their slot. Anything outside this set frees the
#: time for someone else, so it is the definition the conflict check uses --
#: named here rather than inlined, because "does a cancelled appointment still
#: block the slot" is a policy question and it should have one answer.
BLOCKING_APPOINTMENT_STATUSES = frozenset({
    AppointmentStatus.PENDING,
    AppointmentStatus.CONFIRMED,
    AppointmentStatus.RESCHEDULED,
})

#: Terminal: no further provider call will be made for these.
TERMINAL_APPOINTMENT_STATUSES = frozenset({
    AppointmentStatus.CANCELLED,
    AppointmentStatus.NO_SHOW,
    AppointmentStatus.FAILED,
})


class CalendarProviderType(str, enum.Enum):
    """
    Calendar backends with an adapter.

    `GOOGLE_SERVICE_ACCOUNT` is the pre-STEP-6 path, kept as a distinct member
    rather than folded into `GOOGLE`: it authenticates with a platform-wide
    service-account file instead of per-tenant OAuth, which is a different
    security posture and a different set of failure modes. Merging them would
    hide which tenants are still on the shared credential.
    """
    GOOGLE = "google"
    GOOGLE_SERVICE_ACCOUNT = "google_service_account"
    MICROSOFT = "microsoft"
    CALCOM = "calcom"
    INTERNAL = "internal"


class Appointment(Base):
    """
    One booking.

    Columns added in STEP 6 are all nullable or defaulted, so existing rows
    keep working: `status` defaults to CONFIRMED for them (they were created
    under the old code, which only ever wrote a row it believed in), and
    `timezone` is backfilled from the tenant.
    """
    __tablename__ = "appointments"
    __table_args__ = (
        # The concurrency primitive. Two callers racing for the same slot both
        # compute the same key, so the second INSERT loses at the database
        # rather than in application logic. See `service.book`.
        UniqueConstraint(
            "tenant_id", "slot_key", name="uq_appointment_slot"
        ),
        # Requirement 9: a retried booking request must find its own earlier
        # attempt rather than creating a second appointment.
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_appointment_idempotency"
        ),
        Index("ix_appointment_tenant_start", "tenant_id", "starts_at"),
        Index("ix_appointment_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    call_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("calls.id"), nullable=True)

    customer_name: Mapped[str] = mapped_column(String(200))
    customer_phone: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    google_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    # ---- STEP 6 ----------------------------------------------------------

    status: Mapped[AppointmentStatus] = mapped_column(
        Enum(AppointmentStatus), default=AppointmentStatus.PENDING, nullable=False
    )

    #: The IANA zone the customer agreed to, captured at booking time.
    #:
    #: Not derivable from `Tenant.timezone` after the fact: a business that
    #: relocates, or corrects a wrong timezone, would otherwise silently
    #: reinterpret every appointment already in the book. The offset alone is
    #: not enough either -- it does not survive a DST boundary.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)

    provider: Mapped[CalendarProviderType | None] = mapped_column(
        Enum(CalendarProviderType), nullable=True
    )
    #: The provider's own event id. Proof that the booking was accepted; a row
    #: is only CONFIRMED when this is set.
    external_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Which calendar within the provider (Google calendar id, Graph calendar
    #: id, Cal.com event-type id).
    calendar_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)

    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    attendee_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)
    meeting_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    #: Deterministic `{start}|{end}` in UTC. The unique constraint above turns
    #: it into a slot lock. Nullable so pre-STEP-6 rows do not all collide on
    #: NULL -- in both PostgreSQL and SQLite, NULLs are distinct in a UNIQUE
    #: index, which is exactly the behaviour needed for a backfill.
    slot_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Stable across retries of one booking request.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)

    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Free text rather than a FK: the canceller may be a staff user, the
    #: customer on a call, or the provider itself via a webhook.
    cancelled_by: Mapped[str | None] = mapped_column(String(64), nullable=True)

    rescheduled_from: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Scrubbed before storage. Shown to staff, so never a raw provider body.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class Campaign(Base):
    """A batch of outbound calls: cold-call list, follow-up sweep, reminder run."""
    __tablename__ = "campaigns"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    goal: Mapped[str] = mapped_column(String(32), default="qualify")   # qualify|remind|followup|survey
    script_prompt: Mapped[str] = mapped_column(Text, default="")
    opening_line: Mapped[str] = mapped_column(
        Text, default="Hi, this is {agent} calling from {business}. Do you have a quick minute?"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    calls_per_minute: Mapped[int] = mapped_column(Integer, default=2)   # throttle
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    leads: Mapped[list["Lead"]] = relationship(back_populates="campaign")


class Lead(Base):
    """A person to call. Feeds outbound campaigns and gets pushed to the CRM."""
    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("campaigns.id"), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(200), default="")
    phone: Mapped[str] = mapped_column(String(32), index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    company: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    custom_fields: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[LeadStatus] = mapped_column(Enum(LeadStatus), default=LeadStatus.NEW)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    campaign: Mapped["Campaign | None"] = relationship(back_populates="leads")


class Reminder(Base):
    """Scheduled outbound reminder for an appointment. Cuts no-shows ~30%."""
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenants.id"), index=True)
    appointment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("appointments.id"), index=True)
    channel: Mapped[str] = mapped_column(String(16), default="sms")   # sms|call|whatsapp
    send_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    confirmed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


# =============================================================================
# Authentication / RBAC
# =============================================================================

class User(Base):
    """
    A human operator. Always belongs to exactly one tenant -- that binding is
    the root of tenant isolation and is never taken from a request.

    Email is unique GLOBALLY, not per tenant. Login is by email alone with no
    tenant selector, so a duplicate address across tenants would make
    authentication ambiguous. Documented in README under "Tenant isolation".
    """
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email"),
        Index("ix_users_tenant_role", "tenant_id", "role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)   # RFC 5321 max
    full_name: Mapped[str] = mapped_column(String(200), default="")

    # bcrypt output. Never exposed by any serializer -- see UserOut.
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    role: Mapped[UserRole] = mapped_column(Enum(UserRole), default=UserRole.VIEWER)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Bumping this invalidates every access token issued earlier for this user,
    # which is how deactivation and role changes take effect before expiry.
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tenant: Mapped["Tenant"] = relationship()


class RefreshToken(Base):
    """
    Only a SHA-256 digest of the refresh token is stored, so a database leak
    does not hand out sessions. Rotation is enforced: using a token marks it
    used and links the replacement, and replaying a used token revokes the
    whole chain (a standard reuse-detection scheme).
    """
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_active", "user_id", "revoked_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    user_agent: Mapped[str] = mapped_column(String(300), default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")


class AuditLog(Base):
    """
    Security events. Deliberately holds no secret material: no passwords, no
    raw tokens, no API keys. `detail` is free-form JSON for non-sensitive
    context such as which role changed to what.
    """
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_tenant_time", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    action: Mapped[AuditAction] = mapped_column(Enum(AuditAction), nullable=False)
    # Stored even on failed logins, so it must never be a real credential.
    actor_email: Mapped[str] = mapped_column(String(320), default="")
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, index=True, nullable=False
    )


# ============================================================ knowledge ===
#
# STEP 4: tenant-scoped RAG. `Tenant.knowledge_base` (the flat JSON dict) is
# kept and still works -- it holds the handful of one-line facts a business
# types into the dashboard, and those are cheap enough to sit in the system
# prompt. Documents are the new thing: too large to inline, so they are
# chunked, embedded, and retrieved a few chunks at a time.
#
# Uploading a document does NOT mean the agent may answer from all of it. Only
# chunks returned by a retrieval query ever reach the model.


class DocumentStatus(str, enum.Enum):
    """
    Ingestion lifecycle.

    Only READY is searchable. Everything else -- including ARCHIVED -- is
    excluded from retrieval at the SQL level, not filtered afterwards.

    NOTE: SQLAlchemy's Enum() persists the member NAME, so the PostgreSQL type
    `documentstatus` must contain UPLOADED/PROCESSING/READY/FAILED/ARCHIVED.
    """
    UPLOADED = "uploaded"       # stored, not yet processed
    PROCESSING = "processing"   # extraction/chunking/embedding in flight
    READY = "ready"             # searchable
    FAILED = "failed"           # ingestion failed; error_message explains
    ARCHIVED = "archived"       # soft-deleted; never retrieved


#: The only status whose chunks may be returned by a search.
SEARCHABLE_DOCUMENT_STATUSES = frozenset({DocumentStatus.READY})


class DocumentSourceType(str, enum.Enum):
    UPLOAD = "upload"           # a file the tenant uploaded
    TEXT = "text"               # pasted directly into the dashboard
    URL = "url"                 # fetched from a URL (not implemented yet)


class KnowledgeDocument(Base):
    """One uploaded source document belonging to exactly one tenant."""

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        # Deduplication is per tenant: two businesses may legitimately upload
        # the same price list, and that must not collide.
        UniqueConstraint("tenant_id", "content_hash", name="uq_knowledge_doc_hash"),
        Index("ix_knowledge_doc_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    source_type: Mapped[DocumentSourceType] = mapped_column(
        Enum(DocumentSourceType), default=DocumentSourceType.UPLOAD, nullable=False
    )
    #: Storage key, NOT a filesystem path. Resolved by the storage backend so
    #: nothing outside app/knowledge/storage knows where bytes actually live.
    source_uri: Mapped[str | None] = mapped_column(String(500), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(300), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: SHA-256 of the raw bytes. Drives per-tenant deduplication.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus), default=DocumentStatus.UPLOADED, nullable=False
    )
    #: Bumped on every reindex. Chunks record the version they were built from,
    #: so a half-finished reindex can never mix old and new chunks.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    #: Which embedding model produced this document's vectors. If it stops
    #: matching the configured model the document is not searchable until it
    #: is reindexed -- mixing vector spaces silently returns nonsense.
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer, nullable=True)

    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    char_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Free-form, tenant-visible. Never holds credentials or storage paths.
    doc_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    #: Safe, human-readable failure summary. Never a raw stack trace.
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow,
        onupdate=datetime.utcnow, nullable=False,
    )
    ingested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Set when PROCESSING begins, so a stuck job can be detected and reaped.
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    @property
    def is_searchable(self) -> bool:
        return self.status in SEARCHABLE_DOCUMENT_STATUSES


class KnowledgeChunk(Base):
    """
    One retrievable passage.

    `tenant_id` is denormalised onto the chunk on purpose. Retrieval filters on
    it directly in the WHERE clause, so a bug in a join can never widen the
    result set past one tenant.
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint(
            "document_id", "version", "chunk_index", name="uq_knowledge_chunk_slot"
        ),
        Index("ix_knowledge_chunk_tenant_doc", "tenant_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True, nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )

    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: Page number, heading trail, CSV row range -- whatever the extractor knew.
    chunk_metadata: Mapped[dict] = mapped_column(JSON, default=dict)

    #: The vector. Stored as JSON so the same code runs on SQLite in tests and
    #: on PostgreSQL in production; app/knowledge/vectorstore.py upgrades to a
    #: real pgvector column when the extension is available.
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(120), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )

    document: Mapped[KnowledgeDocument] = relationship(back_populates="chunks")


# ===========================================================================
# STEP 5 -- CRM integration layer
#
# The pre-existing CRM support was three columns on `Tenant`
# (`crm_webhook_url`, `crm_type`, `crm_api_key`) and one best-effort POST.
# Those columns are deliberately left in place: `tests/test_outbound.py` still
# exercises the legacy helper, and a migration that drops a column holding a
# live tenant's webhook URL is not something to do in the same change that
# introduces its replacement. Nothing in the new layer reads them.
#
# Five new tables, and the reason each one is separate rather than folded into
# an existing row:
#
# * `CrmIntegration` -- per (tenant, provider) configuration and credentials.
#   Not on `Tenant`, because a tenant may connect several providers at once
#   and because credentials need their own encrypted column with its own
#   access pattern.
# * `CrmEvent`       -- the normalized business event. Persisted *before* any
#   provider is contacted, so a crash between "the call ended" and "the CRM
#   accepted it" loses nothing.
# * `CrmSync`        -- one delivery attempt-set per (event, integration). Fan
#   out to three providers is three rows, so one provider being down cannot
#   mark the others failed.
# * `CrmContactLink` -- the (tenant, provider, identity) -> external id map.
#   This is what stops a second call from the same phone number creating a
#   second contact.
# * `CrmWebhookReceipt` -- inbound replay protection.
# ===========================================================================

class CrmProviderType(str, enum.Enum):
    """
    Providers with an adapter. A closed enum on purpose: the old `crm_type`
    was a free string, so a typo silently selected the generic branch instead
    of failing.
    """
    GOHIGHLEVEL = "gohighlevel"
    HUBSPOT = "hubspot"
    JOBBER = "jobber"
    WEBHOOK = "webhook"


class CrmEntityType(str, enum.Enum):
    CALL = "call"
    LEAD = "lead"
    APPOINTMENT = "appointment"


class CrmEventType(str, enum.Enum):
    """
    Normalized business events.

    The values are dotted wire strings rather than the lowercase-of-the-name
    convention the knowledge enums follow, because these values are published:
    they appear in outbound webhook bodies and in tenant-facing filters.
    `lead.created` is a documented part of the integration contract, so it is
    the value, and the enum member name is what the database stores.
    """
    CALL_COMPLETED = "call.completed"
    CALL_MISSED = "call.missed"
    LEAD_CREATED = "lead.created"
    LEAD_UPDATED = "lead.updated"
    APPOINTMENT_BOOKED = "appointment.booked"
    APPOINTMENT_CANCELLED = "appointment.cancelled"
    TRANSFER_COMPLETED = "transfer.completed"


class CrmSyncStatus(str, enum.Enum):
    """
    PENDING            queued, not yet picked up
    PROCESSING         a worker holds it right now
    SYNCED             the provider acknowledged the write
    FAILED             transient failure, attempts remain
    PERMANENT_FAILURE  will not be retried without human action
    """
    PENDING = "pending"
    PROCESSING = "processing"
    SYNCED = "synced"
    FAILED = "failed"
    PERMANENT_FAILURE = "permanent_failure"


#: Statuses a worker may pick up. `SYNCED` and `PERMANENT_FAILURE` are
#: terminal; `PROCESSING` is excluded so two workers cannot claim one row, and
#: is recovered by the stuck-sync reaper instead.
RETRYABLE_SYNC_STATUSES = frozenset({CrmSyncStatus.PENDING, CrmSyncStatus.FAILED})
TERMINAL_SYNC_STATUSES = frozenset(
    {CrmSyncStatus.SYNCED, CrmSyncStatus.PERMANENT_FAILURE}
)


class CrmIntegration(Base):
    """
    One tenant's connection to one provider.

    `credentials_encrypted` is ciphertext produced by
    `app.integrations.crm.crypto`. It is never selected into an API response --
    the response models in `app/api/integration_routes.py` are allowlists, and
    there is a test asserting the column name does not appear in any response
    body.
    """
    __tablename__ = "crm_integrations"
    __table_args__ = (
        # The isolation primitive. Lookups are (tenant_id, provider); this
        # constraint is what makes that pair a key rather than a filter.
        UniqueConstraint("tenant_id", "provider", name="uq_crm_integration_tenant_provider"),
        Index("ix_crm_integration_tenant_enabled", "tenant_id", "is_enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[CrmProviderType] = mapped_column(
        Enum(CrmProviderType), nullable=False
    )

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: AES-GCM ciphertext of a JSON credential bundle. Opaque here on purpose.
    credentials_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Which key encrypted it, so keys can be rotated without a flag day.
    credentials_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Set when credentials change, so "connected but never tested" is visible.
    credentials_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Non-secret provider settings: GHL location id, HubSpot pipeline, the
    #: outbound webhook URL. Safe to return from the API.
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    #: Tenant-defined mapping of VoxDesk fields to provider custom fields.
    field_mappings: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    #: Which normalized events this integration wants. Empty = all of them.
    subscribed_events: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    #: Requirement 18: transcripts are not shared unless asked for.
    share_transcripts: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    last_health_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_health_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: Operator-facing, already scrubbed of anything secret.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class CrmEvent(Base):
    """
    A business fact, recorded once, independent of any provider.

    Written inside the same transaction as the thing that caused it. Delivery
    is a separate concern with separate rows, which is what makes "the CRM was
    down when the call ended" a recoverable situation rather than a lost lead.
    """
    __tablename__ = "crm_events"
    __table_args__ = (
        # The idempotency primitive. Scoped to the tenant so two tenants can
        # never collide, and so a key from one tenant cannot suppress
        # another's event.
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_crm_event_idempotency"),
        Index("ix_crm_event_tenant_created", "tenant_id", "created_at"),
        Index("ix_crm_event_entity", "tenant_id", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )

    event_type: Mapped[CrmEventType] = mapped_column(Enum(CrmEventType), nullable=False)
    entity_type: Mapped[CrmEntityType] = mapped_column(Enum(CrmEntityType), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    #: Deterministic; see `events.idempotency_key`. Same business fact, same
    #: key, forever.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    #: The normalized payload, already scrubbed. Versioned so an adapter can
    #: tell an old row from a new one after a schema change.
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    payload_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


class CrmSync(Base):
    """
    Delivery state for one event to one integration.

    `external_id` is written in the same commit that sets SYNCED, so a row can
    never claim success without recording what the provider created.
    """
    __tablename__ = "crm_syncs"
    __table_args__ = (
        # One delivery record per (event, integration). This is the constraint
        # that makes a duplicate worker pass a no-op rather than a second POST.
        UniqueConstraint("event_id", "integration_id", name="uq_crm_sync_event_integration"),
        Index("ix_crm_sync_due", "status", "next_attempt_at"),
        Index("ix_crm_sync_tenant_status", "tenant_id", "status"),
        Index("ix_crm_sync_entity", "tenant_id", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("crm_events.id", ondelete="CASCADE"), index=True, nullable=False
    )
    integration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("crm_integrations.id", ondelete="CASCADE"), index=True, nullable=False
    )

    provider: Mapped[CrmProviderType] = mapped_column(Enum(CrmProviderType), nullable=False)
    entity_type: Mapped[CrmEntityType] = mapped_column(Enum(CrmEntityType), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    status: Mapped[CrmSyncStatus] = mapped_column(
        Enum(CrmSyncStatus), default=CrmSyncStatus.PENDING, nullable=False
    )
    #: What the provider created or updated. Proof of the SYNCED claim.
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Scrubbed before storage -- see `errors.safe_message`. Shown to the
    #: tenant, so it must never contain a token or a raw provider body.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: Normalized error class (`rate_limited`, `unauthorized`, ...), for
    #: dashboards that want to group failures without parsing prose.
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class CrmContactLink(Base):
    """
    (tenant, provider, identity) -> provider contact id.

    The identity is a normalized phone or email, hashed, never the raw value:
    this table exists to be looked up quickly and it should not become a
    second copy of the customer list.

    Requirement 19's "never match contacts across tenants" is enforced by
    `tenant_id` being the first column of the unique constraint, not by
    application code remembering to filter.
    """
    __tablename__ = "crm_contact_links"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "identity_hash", name="uq_crm_contact_identity"
        ),
        Index("ix_crm_contact_tenant_provider", "tenant_id", "provider"),
        Index("ix_crm_contact_lead", "tenant_id", "lead_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[CrmProviderType] = mapped_column(Enum(CrmProviderType), nullable=False)

    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"), nullable=True
    )
    #: sha256 of the normalized identity, salted per tenant.
    identity_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    external_contact_id: Mapped[str] = mapped_column(String(255), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class CrmWebhookReceipt(Base):
    """
    Inbound provider events we have already processed.

    Requirement 21 needs replay protection, and replay protection needs
    somewhere durable to remember event ids. Rows are pruned by age, not kept
    forever.
    """
    __tablename__ = "crm_webhook_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "provider_event_id", name="uq_crm_receipt_event"
        ),
        Index("ix_crm_receipt_received", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[CrmProviderType] = mapped_column(Enum(CrmProviderType), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


# ===========================================================================
# STEP 6 -- calendar integration layer
#
# Three tables. The split mirrors STEP 5's, and for the same reason: what a
# tenant configured, what policy applies, and what the provider told us are
# three different lifetimes.
#
# * `CalendarIntegration`  -- per (tenant, provider) connection + encrypted
#   OAuth credentials. Reuses STEP 5's AES-GCM envelope, including its
#   (tenant, provider) associated data.
# * `SchedulingPolicy`     -- business hours, breaks, holidays, buffers.
#   Business policy is *not* provider availability; both are checked, so both
#   need somewhere to live.
# * `CalendarWebhookReceipt` -- inbound provider notification dedupe.
# ===========================================================================

class CalendarIntegration(Base):
    """
    One tenant's connection to one calendar provider.

    Note what is *not* here: a `google_calendar_id` equivalent on `Tenant`.
    That column still exists and still works for the legacy service-account
    path; this table is what a tenant gets when they connect through OAuth.
    """
    __tablename__ = "calendar_integrations"
    __table_args__ = (
        # Same isolation primitive as STEP 5: the pair is a key, not a filter.
        UniqueConstraint(
            "tenant_id", "provider", name="uq_calendar_integration_tenant_provider"
        ),
        Index("ix_calendar_integration_tenant_enabled", "tenant_id", "is_enabled"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[CalendarProviderType] = mapped_column(
        Enum(CalendarProviderType), nullable=False
    )

    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Which provider a booking goes to when the tenant has several connected.
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    #: AES-256-GCM envelope over {"access_token", "refresh_token", ...}.
    #: Identical scheme to CRM credentials -- one cipher, one key ring, one
    #: rotation story. Opaque here.
    credentials_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    credentials_key_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credentials_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: When the access token stops working. Stored in the clear because it is
    #: not secret and the refresh scheduler needs to query on it.
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Non-secret settings: calendar id, Cal.com event-type id, base_url.
    config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    last_health_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_health_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class SchedulingPolicy(Base):
    """
    A tenant's booking rules.

    One row per tenant. Separate from `Tenant` because the pre-STEP-6
    `business_open` / `business_close` / `appointment_minutes` columns are read
    by existing code paths and tests, and widening `Tenant` with fifteen more
    scheduling columns would make an already-large table the home of a second
    subsystem.

    `Tenant`'s three columns remain the fallback: a tenant with no policy row
    behaves exactly as it did before STEP 6.
    """
    __tablename__ = "scheduling_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_scheduling_policy_tenant"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: `{"mon": [["09:00", "12:00"], ["13:00", "17:00"]], "sun": []}`
    #: A list of intervals rather than one open/close pair, because a lunch
    #: break is the single most common reason a booking lands when nobody is
    #: there. An empty list means closed that day.
    weekly_hours: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    #: `["2026-12-25", "2026-01-01"]` -- full-day closures.
    holidays: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    #: `[{"start": "2026-07-01T00:00:00", "end": "2026-07-14T23:59:59"}]`
    #: Local wall-clock. Vacations, refits, a one-off closure.
    blocked_periods: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    slot_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    #: How far apart candidate slots start. Distinct from duration: a 60-minute
    #: appointment offered on a 30-minute grid gives twice the choice.
    slot_interval_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    buffer_before_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    buffer_after_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: The soonest a caller may book. Zero means "in one second", which is
    #: what the pre-STEP-6 code allowed.
    minimum_notice_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    #: The furthest ahead. Stops a caller booking in 2071.
    booking_horizon_days: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    #: How many options the voice agent reads aloud. Reading twelve kills a call.
    max_slots_offered: Mapped[int] = mapped_column(Integer, default=3, nullable=False)

    #: Escape hatch for businesses that genuinely take out-of-hours bookings.
    #: Off by default: requirement 5 says do not book outside business hours
    #: "unless explicitly enabled".
    allow_outside_business_hours: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    #: When the provider cannot be reached, refuse to book rather than
    #: guessing. Default True -- see the audit's F2.
    require_provider_confirmation: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class CalendarWebhookReceipt(Base):
    """
    Inbound calendar notifications already processed.

    Same shape and same reasoning as `CrmWebhookReceipt`: replay protection
    needs somewhere durable to remember provider event ids, and rows are
    pruned by age rather than kept forever.
    """
    __tablename__ = "calendar_webhook_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider", "provider_event_id",
            name="uq_calendar_receipt_event",
        ),
        Index("ix_calendar_receipt_received", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[CalendarProviderType] = mapped_column(
        Enum(CalendarProviderType), nullable=False
    )
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


# ===========================================================================
# STEP 7 -- billing, subscriptions and usage metering
#
# The pre-STEP-7 billing was four columns on `Tenant` and one `+=` on a float.
# Those columns stay (see `docs/BILLING-AUDIT.md`): `minutes_used` is demoted
# from *authority* to *cache*, kept in sync so the dashboard field and any
# external reader keep working, while the number that decides an invoice is
# derived from immutable `UsageEvent` rows.
#
# Five new tables. The split is not decoration -- each has a different
# lifetime and a different trust level:
#
# * `BillingPlan`   -- the catalogue. Server-owned. A tenant never names a
#   price, only a plan code, and the code is resolved here.
# * `Subscription`  -- per (tenant, provider) state, mirrored from the
#   provider. Never authoritative on its own: only a verified webhook or a
#   direct provider read may set it.
# * `UsageEvent`    -- immutable, append-only, idempotency-keyed. The
#   financial record of truth.
# * `UsageSummary`  -- a derived rollup per (tenant, period, metric). A cache
#   that can always be rebuilt from events, which is the property that makes
#   a metering bug recoverable.
# * `BillingWebhookReceipt` -- provider event dedupe and ordering.
# ===========================================================================

class BillingProviderType(str, enum.Enum):
    """
    Billing backends with an adapter.

    `MANUAL` is not a placeholder: it is how a tenant on an invoice-me
    contract, or a development instance with no Stripe account, still gets
    plans, entitlements and metering. Everything except the payment rail works
    identically.
    """
    STRIPE = "stripe"
    MANUAL = "manual"


class SubscriptionStatus(str, enum.Enum):
    """
    Normalized subscription state.

    Stripe's own vocabulary is mapped into this inside
    `app/billing/providers/stripe.py` and nowhere else. The brief is explicit
    that Stripe status strings must not be scattered through business logic,
    and the practical reason is that Stripe has changed them before --
    `incomplete_expired` did not always exist.

    CANCELING is ours, not Stripe's: Stripe expresses "cancel at period end"
    as `active` plus a boolean, which loses the distinction every UI needs.
    """
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELING = "canceling"
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"
    INCOMPLETE_EXPIRED = "incomplete_expired"
    PAUSED = "paused"


#: Statuses that entitle a tenant to use the product. `PAST_DUE` is included
#: deliberately -- see `app/billing/entitlements.py`. Cutting a business off
#: the instant a card expires costs far more goodwill than the few days of
#: service it saves, and Stripe is still retrying the payment.
ENTITLED_SUBSCRIPTION_STATUSES = frozenset({
    SubscriptionStatus.TRIALING,
    SubscriptionStatus.ACTIVE,
    SubscriptionStatus.CANCELING,
    SubscriptionStatus.PAST_DUE,
})

#: No further provider transition is expected.
TERMINAL_SUBSCRIPTION_STATUSES = frozenset({
    SubscriptionStatus.CANCELED,
    SubscriptionStatus.INCOMPLETE_EXPIRED,
})


class BillingInterval(str, enum.Enum):
    MONTH = "month"
    YEAR = "year"


class UsageMetric(str, enum.Enum):
    """
    What is counted.

    Values are the wire names used in the API and in plan configuration, so
    they are part of the published contract.
    """
    VOICE_MINUTE = "voice_minute"
    SMS_SEGMENT = "sms_segment"
    LLM_TOKEN = "llm_token"
    TTS_CHARACTER = "tts_character"


class UsageEventType(str, enum.Enum):
    """
    Why something was counted.

    Distinct from `UsageMetric` because one metric has several sources -- an
    inbound call, an outbound campaign call and a transfer leg all produce
    VOICE_MINUTE -- and the source is what makes a disputed invoice
    answerable.
    """
    VOICE_MINUTE_USED = "voice_minute_used"
    SMS_SEGMENT_USED = "sms_segment_used"
    LLM_TOKEN_USED = "llm_token_used"
    TTS_CHARACTER_USED = "tts_character_used"
    #: A signed correction. Never a destructive edit -- see `UsageEvent`.
    MANUAL_ADJUSTMENT = "manual_adjustment"


class InvoiceStatus(str, enum.Enum):
    DRAFT = "draft"
    OPEN = "open"
    PAID = "paid"
    UNCOLLECTIBLE = "uncollectible"
    VOID = "void"


class BillingPlan(Base):
    """
    The plan catalogue. Server-owned, and the reason a browser can never name
    a price.

    Prices are integer **minor units** (cents), not floats. A float `19.99`
    is not exactly 19.99, and accumulating overage across a few thousand
    fractional minutes in binary floating point produces invoices that do not
    reconcile with themselves. Money is counted, not measured.
    """
    __tablename__ = "billing_plans"
    __table_args__ = (
        UniqueConstraint("code", name="uq_billing_plan_code"),
        Index("ix_billing_plan_active", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    #: Stable identifier used by the API and by config. Never renamed.
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Ordering for a pricing page. Not billing-relevant.
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    currency: Mapped[str] = mapped_column(String(3), default="usd", nullable=False)
    monthly_price_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    annual_price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)

    included_voice_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    included_sms_segments: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    included_llm_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    included_tts_characters: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    #: Overage rates, in **hundredths of a cent** per unit.
    #:
    #: A voice minute at 5c is 500 here; an LLM token at $2/million is 0.0002c,
    #: which cents cannot express at all. Sub-cent granularity is not
    #: fastidiousness -- token and character pricing is genuinely below one
    #: cent per unit, and rounding each unit to a cent would overcharge by
    #: several orders of magnitude.
    overage_voice_minute_millicents: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    overage_sms_millicents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    overage_llm_token_millicents: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    overage_tts_character_millicents: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    #: When false, exceeding the included allowance is refused rather than
    #: metered. Requirement 26: the check happens *before* the provider call.
    overage_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: Non-metered limits: {"team_members": 5, "rag_documents": 100, ...}.
    #: JSON rather than columns because these are the ones that change per
    #: sales conversation, and adding a column per feature would mean a
    #: migration every time someone negotiates.
    feature_entitlements: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    #: Provider price identifiers, keyed by interval:
    #: {"month": "price_FAKE123", "year": "price_FAKE456"}.
    #: **This is the trust boundary.** A checkout request names a plan code
    #: and an interval; the price id comes from here and never from a client.
    provider_price_ids: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    trial_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class Subscription(Base):
    """
    One tenant's subscription with one provider.

    `provider_updated_at` and `provider_event_sequence` exist for requirement
    23. Provider webhooks arrive out of order routinely -- `subscription.updated`
    can land before `checkout.session.completed` -- and applying a stale event
    would downgrade a customer who just upgraded. Every write compares
    timestamps first.
    """
    __tablename__ = "subscriptions"
    __table_args__ = (
        # One subscription per (tenant, provider). The isolation primitive,
        # and what makes "the tenant's subscription" a well-defined phrase.
        UniqueConstraint("tenant_id", "provider", name="uq_subscription_tenant_provider"),
        # A provider subscription id belongs to exactly one tenant. Without
        # this, a webhook carrying an id could be matched to the wrong row.
        UniqueConstraint(
            "provider", "external_subscription_id",
            name="uq_subscription_external_id",
        ),
        Index("ix_subscription_tenant_status", "tenant_id", "status"),
        Index("ix_subscription_period_end", "current_period_end"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("billing_plans.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    provider: Mapped[BillingProviderType] = mapped_column(
        Enum(BillingProviderType), nullable=False
    )

    external_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_subscription_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    #: Which price the provider actually billed. Recorded so a mismatch with
    #: the plan's configured price is detectable rather than invisible.
    external_price_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus), default=SubscriptionStatus.INCOMPLETE, nullable=False
    )
    interval: Mapped[BillingInterval] = mapped_column(
        Enum(BillingInterval), default=BillingInterval.MONTH, nullable=False
    )

    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trial_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trial_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: A downgrade that takes effect at renewal. Requirement 18 asks for an
    #: unambiguous policy: upgrades are immediate, downgrades are scheduled,
    #: and this is where a scheduled one waits.
    pending_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("billing_plans.id", ondelete="SET NULL"), nullable=True
    )
    pending_interval: Mapped[BillingInterval | None] = mapped_column(
        Enum(BillingInterval), nullable=True
    )

    #: The provider's own clock for the last state we applied. Ordering
    #: authority -- see the class docstring.
    provider_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_invoice_status: Mapped[InvoiceStatus | None] = mapped_column(
        Enum(InvoiceStatus), nullable=True
    )
    last_payment_failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Scrubbed before storage. Shown to staff, never a raw provider body.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class UsageEvent(Base):
    """
    One immutable, idempotency-keyed unit of consumption.

    **Append-only.** Nothing in `app/billing` updates or deletes a row here.
    A correction is a new row with a negative `quantity` and
    `event_type = MANUAL_ADJUSTMENT`, which is requirement 35's "never modify
    historical usage quantities destructively" -- and also the only way an
    invoice dispute can ever be answered, because the original figure survives
    alongside the correction.

    `quantity` is an integer in the metric's smallest unit: **seconds** for
    voice, segments for SMS, tokens, characters. Not minutes. Storing 1.5
    minutes as a float and summing a few thousand of them is how a total stops
    matching the sum of its parts.
    """
    __tablename__ = "usage_events"
    __table_args__ = (
        # The idempotency guarantee, at the database rather than in Python.
        # Requirement 14 asks for exactly this backstop.
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_usage_event_idempotency"
        ),
        # The aggregation query: everything for a tenant, period and metric.
        Index("ix_usage_event_rollup", "tenant_id", "billing_period", "metric"),
        Index("ix_usage_event_source", "tenant_id", "source_entity_id"),
        Index("ix_usage_event_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )

    #: `YYYY-MM` of the *billing* period, not the calendar month. Derived from
    #: the subscription's anchor where one exists -- see `billing/periods.py`.
    #: A string because it is a label, compared and grouped, never arithmetic.
    billing_period: Mapped[str] = mapped_column(String(16), nullable=False)

    metric: Mapped[UsageMetric] = mapped_column(Enum(UsageMetric), nullable=False)
    event_type: Mapped[UsageEventType] = mapped_column(
        Enum(UsageEventType), nullable=False
    )

    #: The call, message or document this came from. Makes an invoice line
    #: traceable back to the thing the customer actually did.
    source_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    source_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)

    #: Signed. Negative only for MANUAL_ADJUSTMENT.
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit: Mapped[str] = mapped_column(String(32), nullable=False)

    #: Derived from the business fact, never generated per attempt. See
    #: `billing/metering.py::usage_idempotency_key`.
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)

    #: Safe context: direction, whether the call was transferred, which leg.
    #: Never a transcript, never a credential.
    event_metadata: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )


class UsageSummary(Base):
    """
    A derived rollup per (tenant, period, metric).

    Purely a cache: `metering.rebuild_summary()` can reconstruct any row from
    `UsageEvent` at any time. That property is the point -- it means a bug in
    the summariser is a recoverable inconvenience rather than a corrupted
    ledger, and it is what makes requirement 34's reconciliation possible.

    `finalized` marks a closed period. Once true the figures are what was
    invoiced, and later events for that period are counted but do not silently
    change the number a customer already paid.
    """
    __tablename__ = "usage_summaries"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "billing_period", "metric", name="uq_usage_summary_slot"
        ),
        Index("ix_usage_summary_period", "billing_period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    billing_period: Mapped[str] = mapped_column(String(16), nullable=False)
    metric: Mapped[UsageMetric] = mapped_column(Enum(UsageMetric), nullable=False)

    #: All in the metric's smallest unit, matching `UsageEvent.quantity`.
    included_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    used_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    overage_quantity: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Hundredths of a cent, to match the plan's overage rates.
    estimated_overage_millicents: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    finalized: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Highest threshold already announced (80, 100). Stops a warning firing
    #: on every single call once a tenant is over the line.
    warned_at_percent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class BillingInvoice(Base):
    """
    A mirror of a provider invoice, holding only what is safe to show.

    Deliberately not a general-purpose accounting record: no line items, no
    tax breakdown, no payment method. Requirement 20 asks for metadata and a
    hosted URL, and anything beyond that would be re-implementing Stripe's
    invoice object badly.
    """
    __tablename__ = "billing_invoices"
    __table_args__ = (
        UniqueConstraint(
            "provider", "external_invoice_id", name="uq_billing_invoice_external"
        ),
        Index("ix_billing_invoice_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True, nullable=False
    )
    provider: Mapped[BillingProviderType] = mapped_column(
        Enum(BillingProviderType), nullable=False
    )
    external_invoice_id: Mapped[str] = mapped_column(String(255), nullable=False)

    status: Mapped[InvoiceStatus] = mapped_column(Enum(InvoiceStatus), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="usd", nullable=False)
    amount_due_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    amount_paid_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Stripe's short-lived signed URL. Safe to hand to an authorized user;
    #: it carries no API credential.
    hosted_invoice_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class BillingWebhookReceipt(Base):
    """
    Provider events already processed.

    Same pattern as `CrmWebhookReceipt` and `CalendarWebhookReceipt`. The
    difference here is that a duplicate has financial consequences, so the
    unique constraint is not an optimisation.

    `tenant_id` is nullable because a Stripe event can arrive before the
    customer is linked to a tenant -- the receipt is still recorded so a
    retry of that same event is recognised.
    """
    __tablename__ = "billing_webhook_receipts"
    __table_args__ = (
        # Provider event ids are globally unique, so this is not tenant-scoped.
        UniqueConstraint(
            "provider", "provider_event_id", name="uq_billing_receipt_event"
        ),
        Index("ix_billing_receipt_received", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True
    )
    provider: Mapped[BillingProviderType] = mapped_column(
        Enum(BillingProviderType), nullable=False
    )
    provider_event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    #: The provider's own creation time, used to detect a stale replay.
    provider_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Scrubbed. Kept so a failed event can be investigated and replayed.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, nullable=False
    )

