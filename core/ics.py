"""Add-to-calendar output (§2.5): a plain VEVENT .ics download + a Google Calendar
quick-add link. Outbound only — never a reply mechanism (§1 non-goals)."""

from datetime import UTC, timedelta
from urllib.parse import urlencode

from django.utils import timezone

from .models import Event

# Parties rarely have a firm end; calendars want one. Three hours is a sane default.
DEFAULT_DURATION = timedelta(hours=3)


def _utc(dt) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _esc(text: str) -> str:
    """RFC 5545 text escaping."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def event_window(event: Event) -> tuple:
    start = event.starts_at
    return start, (event.ends_at or start + DEFAULT_DURATION)


def event_ics(event: Event, url: str = "") -> str:
    """A minimal VCALENDAR/VEVENT. The stable per-event UID (§5) means re-adding
    updates the guest's existing calendar entry instead of duplicating it."""
    start, end = event_window(event)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//evently//EN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{event.ics_uid}@evently",
        f"DTSTAMP:{_utc(timezone.now())}",
        f"DTSTART:{_utc(start)}",
        f"DTEND:{_utc(end)}",
        f"SUMMARY:{_esc(event.title)}",
    ]
    if event.location_text:
        lines.append(f"LOCATION:{_esc(event.location_text)}")
    if event.description:
        lines.append(f"DESCRIPTION:{_esc(event.description)}")
    if url:
        lines.append(f"URL:{url}")
    if event.status == Event.Status.CANCELLED:
        lines.append("STATUS:CANCELLED")
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def google_calendar_url(event: Event) -> str:
    start, end = event_window(event)
    params = {
        "action": "TEMPLATE",
        "text": event.title,
        "dates": f"{_utc(start)}/{_utc(end)}",
        "details": event.description,
        "location": event.location_text,
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(
        {k: v for k, v in params.items() if v}
    )
