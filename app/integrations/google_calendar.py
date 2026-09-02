"""Google Calendar via a service account.

Sell tip: ask the client to share their calendar with your service-account email.
That is a 60-second setup on a demo call -- no OAuth screens, no Google review.
"""
from __future__ import annotations

import asyncio
from datetime import datetime

from app.core.config import settings
from app.core.logging import log

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    _GOOGLE_AVAILABLE = True
except ImportError:      # keeps tests runnable without creds
    _GOOGLE_AVAILABLE = False

SCOPES = ["https://www.googleapis.com/auth/calendar"]


class CalendarClient:
    def __init__(self, calendar_id: str | None):
        self.calendar_id = calendar_id
        self._service = None

    def _get_service(self):
        if self._service is None:
            creds = service_account.Credentials.from_service_account_file(
                settings.google_credentials_json, scopes=SCOPES
            )
            self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._service

    async def list_busy(self, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
        if not self.calendar_id or not _GOOGLE_AVAILABLE:
            return []

        def _call():
            body = {
                "timeMin": start.isoformat(),
                "timeMax": end.isoformat(),
                "items": [{"id": self.calendar_id}],
            }
            res = self._get_service().freebusy().query(body=body).execute()
            slots = res["calendars"][self.calendar_id].get("busy", [])
            return [
                (datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"]))
                for s in slots
            ]

        try:
            return await asyncio.to_thread(_call)
        except Exception as exc:
            log.error("calendar.freebusy_failed", error=str(exc))
            return []

    async def create_event(
        self, summary: str, description: str, start: datetime, end: datetime
    ) -> str | None:
        if not self.calendar_id or not _GOOGLE_AVAILABLE:
            return None

        def _call():
            event = {
                "summary": summary,
                "description": description,
                "start": {"dateTime": start.isoformat()},
                "end": {"dateTime": end.isoformat()},
            }
            created = (
                self._get_service()
                .events()
                .insert(calendarId=self.calendar_id, body=event)
                .execute()
            )
            return created.get("id")

        try:
            return await asyncio.to_thread(_call)
        except Exception as exc:
            log.error("calendar.create_failed", error=str(exc))
            return None
