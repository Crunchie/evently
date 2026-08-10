"""Notification content (§2.4): invite, nudge, update, cancellation.

One source of truth for the words guests receive. Every message carries the guest's
unique RSVP link — the channel only ever delivers a link (§4). Text-first with a
minimal HTML wrapper for email.
"""

from django.utils import dateformat, timezone
from django.utils.html import escape

from .models import Invitation

KINDS = ("invite", "nudge", "update", "cancellation", "reminder")


def _when(event) -> str:
    return dateformat.format(timezone.localtime(event.starts_at), "D j M, g:i A")


def render(kind: str, invitation: Invitation, url: str) -> tuple[str, str]:
    """The templated (subject, text) for one message. Snapshotted onto the outbox row at
    enqueue time, and re-rendered at send time for rows the organizer hasn't edited."""
    event = invitation.event
    greeting = invitation.greeting
    when = _when(event)
    where = event.location_text or "location to be confirmed"

    if kind == "invite":
        subject = f"You're invited — {event.title}"
        details = f"📅 {when}\n\n📍 {where}\n\n"
        if event.description:
            details += f"{event.description.strip()}\n\n"
        text = (
            f"Hi {greeting} 👋\n\n"
            f"We would love to see you at our upcoming event: {event.title}.\n\n"
            f"{details}"
            f"See the details and let us know if you can make it:\n{url}"
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
    elif kind == "reminder":
        subject = f"See you soon — {event.title}"
        text = (
            f"Hi {greeting},\n\n"
            f"A quick reminder: {event.title} is coming up — {when}, {where}. "
            f"Looking forward to seeing you!\n\n"
            f"Details (or update your RSVP):\n{url}"
        )
    else:  # pragma: no cover — programming error, not user input
        raise ValueError(f"unknown message kind: {kind}")
    return subject, text


BUTTON_LABELS = {"invite": "Open your invite", "nudge": "Reply now"}


def html_wrap(text: str, url: str, kind: str) -> str:
    """Wrap a plain-text body in the minimal HTML email shell: the body's paragraphs,
    then the RSVP button.

    The raw URL is stripped wherever it appears — the button carries it, and a bare link
    sitting next to a button reads like a bug. It's matched **by content, not position**:
    the templated bodies end with the link, but an organizer-edited body (§7.4) may put it
    anywhere, and dropping the last paragraph blind would eat their words instead.
    """
    paragraphs = []
    for para in text.split("\n\n"):
        cleaned = para.replace(url, "").strip()
        if cleaned:
            paragraphs.append(f'<p style="margin:0 0 14px;line-height:1.55">{escape(cleaned)}</p>')
    button = BUTTON_LABELS.get(kind, "See details")
    return (
        '<div style="font-family:system-ui,-apple-system,sans-serif;max-width:520px;'
        'margin:0 auto;padding:8px;color:#1d1d1f">'
        f"{''.join(paragraphs)}"
        f'<p style="margin:20px 0"><a href="{escape(url)}" '
        'style="display:inline-block;background:#f15c3d;color:#ffffff;padding:12px 22px;'
        f'border-radius:12px;text-decoration:none;font-weight:700">{escape(button)}</a></p>'
        "</div>"
    )


def build_message(kind: str, invitation: Invitation, url: str) -> dict:
    """Subject/text/html for one invitation, straight from the templates. `url` is the
    absolute RSVP link. Outbox rows go through `email_payload` instead, so an edited
    body is honoured — this is the un-edited convenience path."""
    subject, text = render(kind, invitation, url)
    return {"subject": subject, "text": text, "html": html_wrap(text, url, kind)}


def email_payload(subject: str, text: str, url: str, kind: str) -> dict:
    """Subject/text/html for an *already-rendered* body — the outbox's send path, where
    the text may be the organizer's own words rather than the template's."""
    return {"subject": subject, "text": text, "html": html_wrap(text, url, kind)}


def share_payload(kind: str, invitation: Invitation, url: str) -> dict:
    """The assisted-channel payload (§6): `text` for clipboard / wa.me (link
    included), plus `share_text` + `url` for `navigator.share`, which appends the
    URL itself — passing both would double the link in some targets."""
    return share_payload_from(render(kind, invitation, url)[1], url)


def share_payload_from(text: str, url: str) -> dict:
    """`share_payload` for a body that's already rendered (an outbox row's, possibly
    edited). The URL is appended if the body somehow lost it, so the one invariant that
    matters — every message carries the link (§4) — can't be edited away."""
    if url not in text:
        text = f"{text.rstrip()}\n\n{url}"
    return {"text": text, "share_text": text.replace(url, "").rstrip(), "url": url}
