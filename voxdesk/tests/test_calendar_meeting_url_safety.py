"""
`meeting_url` scheme allowlist.

Providers hand this string over verbatim -- Google's `hangoutLink`,
Microsoft's `joinUrl`, and for Cal.com `meetingUrl` *or the free-text
`location` field*. The dashboard renders it as a link, so a value of
`javascript:...` would be stored XSS. `CalendarEvent` normalises the
unusable ones away at construction, which is the single point every
provider response passes through.

The dashboard applies the same allowlist independently; neither layer
trusts the other.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.integrations.calendar.models import CalendarEvent

START = datetime(2026, 9, 8, 15, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=30)


def build(meeting_url):
    return CalendarEvent(
        external_id="evt_1", start=START, end=END, meeting_url=meeting_url
    )


@pytest.mark.parametrize("url", [
    "https://meet.google.com/abc-defg-hij",
    "https://teams.microsoft.com/l/meetup-join/19%3ameeting_abc",
    "https://calendar.example.com/event/123",
    "https://cal.example.com/join?token=a-b_c.d~e&x=1",
    "https://example.com:8443/join/room",
])
def test_real_provider_urls_are_preserved_exactly(url):
    # Byte-for-byte: re-serialising would silently rewrite a provider's URL.
    assert build(url).meeting_url == url


@pytest.mark.parametrize("url", [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "  javascript:alert(1)",
    "\njavascript:alert(1)",
    "\tjavascript:alert(document.domain)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "//evil.example.com/join",
    "http://plain.example.com/join",
    "ftp://files.example.com/x",
    "not a url at all",
    "Office, 2nd floor",          # a real Cal.com `location` value
    "",
    "   ",
    "https://",                   # scheme but no host
])
def test_dangerous_or_unusable_urls_become_none(url):
    assert build(url).meeting_url is None


def test_none_stays_none():
    assert build(None).meeting_url is None


def test_non_string_is_rejected_rather_than_crashing():
    assert build(12345).meeting_url is None


def test_normalisation_never_fails_a_booking():
    # A hostile URL must not raise: the appointment is still valid, it just
    # has no meeting link.
    event = build("javascript:alert(1)")
    assert event.external_id == "evt_1"
    assert event.start == START
    assert event.meeting_url is None


def test_surrounding_whitespace_is_trimmed_on_a_valid_url():
    assert build("  https://ok.example.com/x  ").meeting_url == "https://ok.example.com/x"