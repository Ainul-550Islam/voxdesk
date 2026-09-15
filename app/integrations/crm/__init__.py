"""
Provider-agnostic CRM integration layer (STEP 5).

    VoxDesk call/lead/appointment
      -> normalized CrmEvent          (events.emit, inside the caller's txn)
      -> CrmSync row per integration  (queued, nothing sent yet)
      -> provider adapter             (providers/, one per vendor)
      -> CRM API
      -> persisted sync result        (SYNCED + external_id, atomically)
      -> retry / idempotency          (retry.py, events.idempotency_key)
      -> structured audit log         (service.py, no secrets)

The legacy helpers from the pre-STEP-5 `app/integrations/crm.py` are still
importable from this package. They now live in `legacy.py` and nothing in the
new layer calls them; they remain only so the tests written against them keep
passing and so a deployment mid-migration does not lose its webhook.
"""
from app.integrations.crm.legacy import (  # noqa: F401
    _headers,
    build_payload,
    push,
    to_gohighlevel,
)

__all__ = ["build_payload", "push", "to_gohighlevel", "_headers"]