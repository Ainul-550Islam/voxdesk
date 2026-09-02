"""
Provider-agnostic scheduling layer (STEP 6).

    caller request
      -> nlp.parse_request        deterministic date/time, never the LLM
      -> policy                   business hours, breaks, holidays, buffers
      -> provider free/busy       network, and a failure is never emptiness
      -> VoxDesk appointments     our own commitments
      -> service.book             slot lock in the database, then the provider
      -> provider confirmation    CONFIRMED requires an external event id
      -> CRM event                via STEP 5's abstraction, never a direct call
      -> reminder                 idempotent, replaced on reschedule

The pre-STEP-6 `app/integrations/google_calendar.py` is untouched and still
works; `providers/internal.py` wraps it so existing tenants keep running while
they migrate to per-tenant OAuth.
"""

