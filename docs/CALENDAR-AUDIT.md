# STEP 6 pre-work: audit of the existing calendar / scheduling code

Written **before** anything was modified, and every claim below was executed
rather than read.

## What existed

| Piece | Lines | Where |
|---|---|---|
| `CalendarClient` (Google, service account) | 85 | `app/integrations/google_calendar.py` |
| `check_availability` / `book_appointment` | ~100 | `app/agent/functions.py` |
| `Appointment` model | 10 fields | `app/db/models.py:288` |
| `Reminder` model + dispatch | 98 | `app/db/models.py:357`, `app/integrations/reminders.py` |
| Tenant scheduling config | 5 columns | `timezone`, `business_open`, `business_close`, `appointment_minutes`, `reminder_*` |
| Appointment API | **none** | — |
| Tests | 6 | `tests/test_availability.py` |

## Findings

### The two that make the product lie to a caller

**F1 — VoxDesk tells the caller "booked" when the calendar rejected it.**
`create_event()` catches every exception and returns `None`. `book_appointment`
does not check the return value: it writes the `Appointment` row with
`google_event_id=None` and answers *"Booked for Tuesday at 3."* Executed:

```
create_event when the provider is unreachable -> None
=> the caller is told the appointment is booked; nothing exists on the calendar.
```

This is precisely what the STEP 6 brief forbids. Severity: the business misses
the customer, the customer arrives to a closed door, and nothing in the system
records that anything went wrong.

**F2 — A calendar outage silently converts to "everything is free."**
`list_busy()` returns `[]` on any exception. Every caller of it treats `[]` as
"no conflicts". So when Google is down, availability offers slots that are
already taken, and the pre-booking conflict re-check passes unconditionally.
Failure and emptiness are the same value.

### Correctness

| # | Finding | Evidence |
|---|---|---|
| F3 | **Reminders due in the next 4–5 hours are silently dropped.** `reminders.py:49` compares `send_at` against `datetime.utcnow().replace(tzinfo=send_at.tzinfo)` — a naive UTC clock reading relabelled as the appointment's timezone. Measured for `America/New_York`: the code believes "now" is **3:59:59 ahead** of reality, so `send_at <= now` is `True` and the reminder is never created. | executed |
| F4 | **No conflict check against VoxDesk's own appointments.** Only Google free/busy is consulted. Two callers booking the same slot both succeed whenever Google has not yet propagated the first event — and always, if Google is unreachable (F2). | `functions.py:238` |
| F5 | **Time-of-check/time-of-use.** `book_appointment` re-runs `list_busy`, then creates. Nothing holds the slot between the two. | `functions.py:238-247` |
| F6 | **No `AppointmentStatus`.** No cancel, no reschedule, no no-show, no confirmation. An appointment row exists or it does not. | `models.py:288` |
| F7 | **The appointment's timezone is not stored.** Only `Tenant.timezone`. A tenant relocating or correcting their timezone silently reinterprets every historical appointment. | `models.py:288` |
| F8 | **The LLM does all date arithmetic.** `check_availability(date="%Y-%m-%d")` and `book_appointment(starts_at=ISO)` require the model to have already resolved "next Tuesday at 3" — including DST. The brief explicitly forbids trusting the model for this. | `functions.py:190`, `:228` |
| F9 | **Zero minimum notice.** `cursor > datetime.now(self.tz)` offers a slot starting in one second. | `functions.py:210` |
| F10 | **Duration and slot step are the same number.** `appointment_minutes` is both. No buffer before/after, no booking horizon. | `functions.py:201` |
| F11 | **Business hours are one open/close pair for all seven days.** No per-weekday, no breaks, no holidays, no blocked dates. Sunday is a working day. | `models.py:161` |
| F12 | `slots[:3]` hard-coded in two places. | `functions.py:214` |

### Providers and credentials

| # | Finding |
|---|---|
| F13 | **One provider: Google, service-account only.** No OAuth flow, no authorization callback, no access token, no refresh token, no expiry handling. `google_credentials_json` is a filesystem path to a shared JSON key. |
| F14 | **The service account is platform-wide, not per tenant.** Every tenant's calendar is reached with the same credential; isolation rests entirely on `tenant.google_calendar_id` being correct. There is no per-tenant calendar credential to isolate. |
| F15 | **No Microsoft Graph, no Cal.com, no provider abstraction.** `CalendarClient` is instantiated directly inside `FunctionHandlers.__init__`. |
| F16 | **No error normalization.** 401, 403, 404, 409, 429 and a socket timeout are all `except Exception -> return None`. Nothing can distinguish "retry this" from "the authorization was revoked". |
| F17 | **No retry at all**, transient or otherwise. |
| F18 | **No provider webhooks.** An event deleted in Google is invisible to VoxDesk forever. |

### What already works and must be preserved

* `Tenant.timezone` exists, defaults to `America/New_York`, and
  `FunctionHandlers` correctly builds a `ZoneInfo` from it.
* `datetime.combine(day, business_open, tzinfo=self.tz)` is the right way to
  construct a wall-clock local time.
* The `Reminder` table and `run_reminder_tick` worker loop exist and are wired
  into `scripts/scheduler.py`.
* STEP 5's `crm_hooks.on_appointment_booked` is already called from
  `book_appointment`, inside the transaction, and works.
* `tests/test_availability.py` — 6 tests, all passing.

## Direct answers to the questions the brief asked

**What calendar functionality already exists?**
Free/busy lookup and event creation against one Google calendar, plus in-memory
slot generation.

**Is current booking actually provider-backed?**
No. It is provider-*attempted*. The database row and the spoken confirmation
are written regardless of what the provider did.

**Is only Google supported?**
Yes, and only through a service account rather than OAuth.

**Is appointment persistence authoritative?**
It is the only record, but it is not authoritative in the sense that matters:
it can claim an event exists when it does not, and it is never consulted when
checking for conflicts.

**Is timezone stored?**
On the tenant, yes. On the appointment, no.

**Can duplicate bookings happen?**
Yes — three separate ways: the TOCTOU window (F5), the absence of any check
against VoxDesk's own appointments (F4), and the outage-becomes-free failure
mode (F2).

**Is availability provider-derived?**
Partly. It is derived from Google free/busy alone, ignoring VoxDesk's own
bookings and any business policy beyond one open/close pair.

**Do reminders exist?**
Yes, and they are wired into the worker — but the scheduling comparison is
wrong (F3), so a large class of them is never created.

## What STEP 6 keeps

`app/integrations/google_calendar.py` stays exactly where it is and keeps its
current behaviour, so `tests/test_availability.py` continues to pass
unmodified. The new layer does not call it. Retiring it is a follow-up once
tenants have been migrated onto `CalendarIntegration` rows.
