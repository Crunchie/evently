"""Notification content (§2.4): invite, nudge, update, cancellation.

One source of truth for the words guests receive. Every message carries the guest's
unique RSVP link — the channel only ever delivers a link (§4). Text-first with a
minimal HTML wrapper for email.
"""

from django.utils import dateformat, timezone
from django.utils.html import escape

from .models import Invitation

KINDS = ("invite", "nudge", "update", "cancellation")


def _when(event) -> str:
    return dateformat.format(timezone.localtime(event.starts_at), "D j M, g:i A")


def _subject_and_text(kind: str, invitation: Invitation, url: str) -> tuple[str, str]:
    event = invitation.event
    greeting = invitation.greeting
    host = event.host_display or "your hosts"
    when = _when(event)
    where = event.location_text or "location to be confirmed"

    if kind == "invite":
        subject = f"You're invited — {event.title}"
        text = (
            f"Hi {greeting} 👋\n\n"
            f"{host} would love to see you at {event.title} — {when}, {where}.\n\n"
            f"See the details and let them know if you can make it:\n{url}"
        )
    elif kind == "nudge":
        subject = f"Still hoping you can make it — {event.title}"
        text = (
            f"Hi {greeting},\n\n"
            f"Just a friendly nudge about {event.title} ({when}) — "
            f"it'd be great to know either way.\n\n"
            f"Tap to reply (takes 5 seconds):\n{url}"
        )
    elif kind == "update":
        subject = f"Update — {event.title}"
        text = (
            f"Hi {greeting},\n\n"
            f"The details for {event.title} have changed. "
            f"It's now: {when}, {where}.\n\n"
            f"Latest details and your RSVP:\n{url}"
        )
    elif kind == "cancellation":
        subject = f"Cancelled — {event.title}"
        text = (
            f"Hi {greeting},\n\n"
            f"Sorry — {event.title} ({when}) has been cancelled.\n\n"
            f"Details:\n{url}"
        )
    else:  # pragma: no cover — programming error, not user input
        raise ValueError(f"unknown message kind: {kind}")
    return subject, text


def _html(text: str, url: str, button: str) -> str:
    paragraphs = "".join(
        f'<p style="margin:0 0 14px;line-height:1.55">{escape(p)}</p>'
        for p in text.split("\n\n")[:-1]  # last paragraph is the raw URL — replaced by button
    )
    return (
        '<div style="font-family:system-ui,-apple-system,sans-serif;max-width:520px;'
        'margin:0 auto;padding:8px;color:#1d1d1f">'
        f"{paragraphs}"
        f'<p style="margin:20px 0"><a href="{escape(url)}" '
        'style="display:inline-block;background:#f15c3d;color:#ffffff;padding:12px 22px;'
        f'border-radius:12px;text-decoration:none;font-weight:700">{escape(button)}</a></p>'
        "</div>"
    )


def build_message(kind: str, invitation: Invitation, url: str) -> dict:
    """Subject/text/html for one invitation. `url` is the absolute RSVP link."""
    subject, text = _subject_and_text(kind, invitation, url)
    button = {"invite": "Open your invite", "nudge": "Reply now"}.get(kind, "See details")
    return {"subject": subject, "text": text, "html": _html(text, url, button)}
