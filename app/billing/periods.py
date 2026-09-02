"""
Billing periods.

Requirement 17, and the sentence that drives the whole module: *"Never use
server local time as billing-period authority."*

Three rules:

**A period comes from the subscription, not the calendar.** A tenant who
subscribed on the 15th is billed the 15th to the 15th. Rolling their usage up
by calendar month would split every one of their periods across two buckets
and make the invoice unreconcilable with the usage page.

**Everything is UTC.** The label `2026-03` means "the period that *started* in
March UTC", not "March in the tenant's timezone". Two tenants in different
zones on the same plan get the same label for the same subscription period,
which is what makes cross-tenant reporting possible at all.

**Month arithmetic clamps, it does not overflow.** A subscription anchored on
the 31st has no 31st in February. Stripe clamps to the last day of the month
and so does this — `add_months(Jan 31, 1)` is `Feb 28`, not `Mar 3`. Getting
that wrong moves a customer's renewal date permanently, one month at a time.

DST is a non-issue *because* everything is UTC, and that is the reason for the
choice rather than a happy accident: an anchor stored as a local wall clock
would shift by an hour twice a year and eventually cross a day boundary.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

UTC = timezone.utc

#: `YYYY-MM`. Validated rather than trusted, because it reaches a query.
PERIOD_LABEL = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class PeriodError(ValueError):
    """A malformed period label or an impossible anchor."""


@dataclass(frozen=True)
class BillingPeriod:
    """
    A half-open interval `[start, end)` and the label usage is filed under.

    Half-open for the same reason appointment windows are: a period ending at
    exactly 00:00 on the 15th and the next beginning at 00:00 on the 15th must
    not both contain that instant, or one call gets billed twice.
    """

    label: str
    start: datetime
    end: datetime

    def __post_init__(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise PeriodError("billing period bounds must be timezone-aware")
        if self.end <= self.start:
            raise PeriodError("a billing period must end after it starts")

    def contains(self, moment: datetime) -> bool:
        return self.start <= _aware(moment) < self.end

    @property
    def days(self) -> int:
        return (self.end - self.start).days


def _aware(value: datetime) -> datetime:
    """
    Attach UTC to a naive value.

    Safe *here* and only here: everything this application stores is UTC, and
    SQLite hands back naive datetimes even for `DateTime(timezone=True)`.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def now_utc() -> datetime:
    """
    The current instant, aware.

    Exists so no billing code calls `datetime.utcnow()`, which returns a naive
    value that compares wrongly against aware ones. That mistake cost the
    reminder scheduler every reminder due inside the tenant's UTC offset
    (STEP 6, audit F3); in billing it would misfile usage across a period
    boundary.
    """
    return datetime.now(UTC)


def add_months(moment: datetime, months: int) -> datetime:
    """
    Add calendar months, clamping the day.

    `Jan 31 + 1 month` is `Feb 28` (or 29), not `Mar 3`. Naive day arithmetic
    would walk a 31st-anchored subscription forward through the calendar until
    it renewed on a different date than the customer agreed to.
    """
    moment = _aware(moment)
    zero_based = moment.month - 1 + months
    year = moment.year + zero_based // 12
    month = zero_based % 12 + 1
    day = min(moment.day, calendar.monthrange(year, month)[1])
    return moment.replace(year=year, month=month, day=day)


def period_label(start: datetime) -> str:
    """
    The bucket a period's usage is filed under: the UTC year-month it began.

    Deliberately the *start*, so a period running 15 March to 15 April is
    entirely `2026-03`. Labelling by end date, or splitting across two
    buckets, would mean the usage page and the invoice never agree.
    """
    start = _aware(start).astimezone(UTC)
    return f"{start.year:04d}-{start.month:02d}"


def parse_label(label: str) -> tuple[int, int]:
    if not PERIOD_LABEL.match(label or ""):
        raise PeriodError(f"{label!r} is not a YYYY-MM billing period")
    year, month = label.split("-")
    return int(year), int(month)


def calendar_period(moment: datetime | None = None) -> BillingPeriod:
    """
    The calendar month containing `moment`, in UTC.

    The fallback for a tenant with no subscription -- a development instance,
    a trial before checkout, an invoice-me contract. Usage is still metered
    and still bucketed; it simply has no provider-anchored period to follow.
    """
    moment = _aware(moment or now_utc()).astimezone(UTC)
    start = moment.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return BillingPeriod(
        label=period_label(start), start=start, end=add_months(start, 1)
    )


def subscription_period(
    subscription, moment: datetime | None = None
) -> BillingPeriod:
    """
    The period a subscription is currently in.

    Prefers the provider's own `current_period_start` / `current_period_end`,
    because those are what the invoice will be cut against and any local
    calculation that disagrees with them is wrong by definition.

    Falls back to projecting forward from the anchor when the provider bounds
    are absent or stale -- which happens between a checkout completing and the
    first `customer.subscription.updated` arriving, a window in which calls
    are still being made and still have to be filed somewhere.
    """
    moment = _aware(moment or now_utc())

    start = getattr(subscription, "current_period_start", None)
    end = getattr(subscription, "current_period_end", None)

    if start is not None and end is not None:
        start, end = _aware(start), _aware(end)
        if start < end and start <= moment < end:
            return BillingPeriod(label=period_label(start), start=start, end=end)

        # The provider bounds exist but do not contain `moment`: a renewal
        # happened and we have not seen the webhook yet. Walk the anchor
        # forward rather than filing the usage into a period that has closed.
        if start < end and moment >= end:
            return _project(start, end - start, moment)

    anchor = (
        getattr(subscription, "current_period_start", None)
        or getattr(subscription, "trial_start", None)
        or getattr(subscription, "created_at", None)
    )
    if anchor is None:
        return calendar_period(moment)

    return _project(_aware(anchor), None, moment)


def _project(anchor: datetime, length: timedelta | None, moment: datetime) -> BillingPeriod:
    """
    Walk monthly periods forward from `anchor` until one contains `moment`.

    Month-by-month rather than dividing an elapsed time by a length, because
    months are not a fixed duration. Bounded at 600 iterations (fifty years)
    so a corrupt anchor cannot spin forever -- if it trips, the calendar month
    is a defensible answer and the caller is not left hanging.
    """
    if length is not None and length > timedelta(days=45):
        # An annual subscription. Keep monthly usage buckets anyway: an
        # annual plan still has a monthly included allowance, and rolling a
        # year of calls into one bucket would make "you used 480 of 500
        # minutes" meaningless.
        pass

    start = anchor
    for _ in range(600):
        end = add_months(start, 1)
        if start <= moment < end:
            return BillingPeriod(label=period_label(start), start=start, end=end)
        if moment < start:
            # The anchor is in the future -- a scheduled subscription. Use the
            # calendar month; nothing is being billed yet.
            return calendar_period(moment)
        start = end

    return calendar_period(moment)


def period_for(subscription, moment: datetime | None = None) -> BillingPeriod:
    """
    The period any usage at `moment` belongs to.

    The single entry point the metering layer uses, so "which bucket" has
    exactly one answer in the codebase.
    """
    if subscription is None:
        return calendar_period(moment)
    return subscription_period(subscription, moment)


def previous_period(period: BillingPeriod) -> BillingPeriod:
    """The period immediately before this one. Used by reconciliation."""
    length_months = 1
    start = add_months(period.start, -length_months)
    return BillingPeriod(label=period_label(start), start=start, end=period.start)


def elapsed_fraction(period: BillingPeriod, moment: datetime | None = None) -> float:
    """
    How far through the period we are, 0.0 to 1.0.

    Used by the threshold logic to distinguish "80% of the allowance on day 2"
    -- which is worth warning about -- from "80% on day 27", which is a tenant
    using what they paid for.
    """
    moment = _aware(moment or now_utc())
    if moment <= period.start:
        return 0.0
    if moment >= period.end:
        return 1.0
    return (moment - period.start).total_seconds() / (
        period.end - period.start
    ).total_seconds()

