"""Notification service (Batch 01 enterprise expansion).

Templated, priority-ordered, deduplicated notifications that route through the
infrastructure that already exists:

* ``SMS`` goes through ``app.integrations.notifications.send_sms`` (Twilio),
  after a compliance content check — never around it.
* ``IN_APP`` and ``INTERNAL_ALERT`` are delivered locally (state flip).
* ``WEBHOOK`` is SSRF-validated and *prepared*; dispatch is deferred until a
  delivery worker exists (honest, reported gap).
* ``EMAIL`` is declared but suppressed — there is deliberately no new email
  provider in this batch.

Deduplication is deterministic (``dedupe_key``), and ``Notification.safe_repr``
is the only string form allowed in logs — a full rendered body or a raw
recipient contact is never logged.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from app.core import compliance
from app.core.logging import log
from app.core.ssrf import OutboundUrlError, validate_outbound_url
from app.domain.notification_models import (
    DeliveryResult,
    DeliveryState,
    EventSource,
    Notification,
    NotificationChannel,
    NotificationPriority,
    NotificationTemplate,
    PreferenceSet,
    Recipient,
    dedupe_key,
    with_attempt,
)
from app.integrations.notifications import send_sms

#: tenant_id -> template_id -> template
_TEMPLATES: dict[str, dict[str, NotificationTemplate]] = {}
#: tenant_id -> notification_id -> notification
_NOTIFICATIONS: dict[str, dict[str, Notification]] = {}
#: tenant_id -> dedupe_key -> notification_id (dedupe index)
_DEDUPE: dict[str, dict[str, str]] = {}

MAX_BACKOFF_SECONDS = 3600


def _slot(store: dict, *keys: str) -> dict:
    node: dict = store
    for key in keys:
        node = node.setdefault(key, {})
    return node


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- templates ---

def validate_template(template: NotificationTemplate) -> list[str]:
    return template.validate()


def create_template(tenant_id: str, template: NotificationTemplate) -> NotificationTemplate:
    if template.tenant_id != tenant_id:
        raise ValueError("template does not belong to this tenant")
    problems = validate_template(template)
    if problems:
        raise ValueError("; ".join(problems))
    _slot(_TEMPLATES, tenant_id)[template.id] = template
    return template


def get_template(tenant_id: str, template_id: str) -> NotificationTemplate:
    template = _slot(_TEMPLATES, tenant_id).get(template_id)
    if template is None:
        raise KeyError("template not found")
    return template


def list_templates(tenant_id: str) -> list[NotificationTemplate]:
    return sorted(_slot(_TEMPLATES, tenant_id).values(), key=lambda t: t.name)


def render(template: NotificationTemplate, variables: dict[str, str]) -> str:
    return template.render(variables or {})


# -------------------------------------------------------------- preferences ---

def resolve_preferences(
    preference: PreferenceSet,
    channel: NotificationChannel,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Decide whether a channel is allowed right now for this preference set."""
    if channel is not preference.channel:
        return False, "channel not enabled for recipient"
    if not preference.enabled:
        return False, "recipient disabled this channel"
    if preference.is_quiet(now):
        return False, "recipient is in quiet hours"
    return True, ""


# --------------------------------------------------------------- lifecycle ---

def create_notification(
    tenant_id: str,
    *,
    template: NotificationTemplate,
    recipient: Recipient,
    event_source: EventSource,
    priority: NotificationPriority = NotificationPriority.NORMAL,
    business_key: str = "",
    variables: dict[str, str] | None = None,
) -> Notification:
    """Render + create a notification, deduplicating on the business key."""
    if template.tenant_id != tenant_id:
        raise ValueError("template does not belong to this tenant")
    key = dedupe_key(tenant_id, template.id, event_source, business_key)
    existing_id = _slot(_DEDUPE, tenant_id).get(key)
    if existing_id is not None:
        existing = _slot(_NOTIFICATIONS, tenant_id).get(existing_id)
        if existing is not None:
            return existing
    body = render(template, variables or {})
    notification = Notification(
        id=key,
        tenant_id=tenant_id,
        template_id=template.id,
        channel=template.channel,
        recipient=recipient,
        event_source=event_source,
        priority=priority,
        dedupe_key=key,
        rendered_body=body,
    )
    _slot(_NOTIFICATIONS, tenant_id)[notification.id] = notification
    _slot(_DEDUPE, tenant_id)[key] = notification.id
    log.info("notification.created", tenant_id=tenant_id, notification=notification.safe_repr())
    return notification


def enqueue_system_notification(
    tenant_id: str,
    template_id: str,
    business_key: str,
    *,
    variables: dict[str, str] | None = None,
    priority: NotificationPriority = NotificationPriority.NORMAL,
) -> Notification | None:
    """Convenience used by the workflow engine: system -> tenant in-app.

    Returns ``None`` (rather than raising) when the template does not exist, so
    a workflow action with a missing template fails soft instead of crashing
    the execution.
    """
    template = _slot(_TEMPLATES, tenant_id).get(template_id)
    if template is None:
        log.warning("notification.template_missing", tenant_id=tenant_id, template_id=template_id)
        return None
    recipient = Recipient(kind="user", target="system", user_id="")
    return create_notification(
        tenant_id,
        template=template,
        recipient=recipient,
        event_source=EventSource.SYSTEM,
        priority=priority,
        business_key=business_key,
        variables=variables,
    )


async def deliver(notification: Notification) -> DeliveryResult:
    """Deliver one notification through the channel's existing infrastructure."""
    if notification.channel is NotificationChannel.IN_APP:
        return DeliveryResult(DeliveryState.DELIVERED, notification.channel, "in-app delivered")
    if notification.channel is NotificationChannel.INTERNAL_ALERT:
        return DeliveryResult(DeliveryState.DELIVERED, notification.channel, "internal alert logged")
    if notification.channel is NotificationChannel.SMS:
        body = notification.rendered_body
        issues = compliance.check_message(body)
        if any(issue.severity == "error" for issue in issues):
            return DeliveryResult(DeliveryState.SUPPRESSED, notification.channel,
                                  "compliance check failed")
        sent = await send_sms(notification.recipient.target, body)
        if sent:
            return DeliveryResult(DeliveryState.SENT, notification.channel, "sms sent")
        return DeliveryResult(DeliveryState.FAILED, notification.channel, "sms provider error")
    if notification.channel is NotificationChannel.WEBHOOK:
        try:
            validate_outbound_url(notification.recipient.target, require_https=True)
        except OutboundUrlError as exc:
            return DeliveryResult(DeliveryState.SUPPRESSED, notification.channel, str(exc))
        return DeliveryResult(
            DeliveryState.PENDING, notification.channel,
            "webhook validated; dispatch requires the delivery worker (batch 02)",
        )
    if notification.channel is NotificationChannel.EMAIL:
        return DeliveryResult(DeliveryState.SUPPRESSED, notification.channel,
                              "no email provider configured")
    return DeliveryResult(DeliveryState.SUPPRESSED, notification.channel, "unsupported channel")


def record_delivery(tenant_id: str, notification: Notification, result: DeliveryResult) -> Notification:
    updated = replace(
        notification,
        delivery_state=result.state,
        error_summary=result.detail[:500],
        sent_at=_now() if result.delivered else notification.sent_at,
    )
    _slot(_NOTIFICATIONS, tenant_id)[notification.id] = updated
    log.info("notification.delivered", tenant_id=tenant_id, notification=updated.safe_repr())
    return updated


def retry(tenant_id: str, notification_id: str) -> Notification:
    """Advance the retry counter with bounded exponential backoff."""
    notification = _slot(_NOTIFICATIONS, tenant_id).get(notification_id)
    if notification is None:
        raise KeyError("notification not found")
    backoff = min(MAX_BACKOFF_SECONDS, 60 * (2 ** min(notification.attempts, 6)))
    updated = with_attempt(notification, error="retry scheduled")
    updated = replace(
        updated,
        next_attempt_at=(datetime.now(timezone.utc) + timedelta(seconds=backoff)).isoformat(),
    )
    _slot(_NOTIFICATIONS, tenant_id)[notification_id] = updated
    return updated


def mark_read(tenant_id: str, notification_id: str) -> Notification:
    notification = _slot(_NOTIFICATIONS, tenant_id).get(notification_id)
    if notification is None:
        raise KeyError("notification not found")
    # read state is derived from delivery_state + a read flag in the overlay
    _READ.setdefault(tenant_id, set()).add(notification_id)
    return notification


def mark_unread(tenant_id: str, notification_id: str) -> Notification:
    notification = _slot(_NOTIFICATIONS, tenant_id).get(notification_id)
    if notification is None:
        raise KeyError("notification not found")
    _READ.setdefault(tenant_id, set()).discard(notification_id)
    return notification


_READ: dict[str, set[str]] = {}


def is_read(tenant_id: str, notification_id: str) -> bool:
    return notification_id in _READ.get(tenant_id, set())


def history(tenant_id: str, *, unread_only: bool = False) -> list[Notification]:
    notifications = sorted(
        _slot(_NOTIFICATIONS, tenant_id).values(),
        key=lambda n: n.sent_at or "",
        reverse=True,
    )
    if unread_only:
        notifications = [n for n in notifications if not is_read(tenant_id, n.id)]
    return notifications
