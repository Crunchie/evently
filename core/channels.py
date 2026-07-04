"""Outbound dispatcher + the email (Resend) channel (§4/§6/§9).

Channels come in two flavors: **automated** (the app calls an API — email, here) and
**assisted** (the app prepares a share payload and a human taps send — Phase 5). Every
channel delivers the same thing: the guest's unique RSVP link.

Sends are **synchronous in the request** (§9 — no cron, no queue): the view calls
`dispatch_email()` and gets per-delivery outcomes back immediately. `Delivery` rows are
the audit record; bounces arrive later via the signature-verified webhook (views).
"""

import resend
from django.conf import settings
from django.utils import timezone

from .messaging import build_message
from .models import ContactChannel, Delivery, Event, Invitation

RESEND_BATCH_LIMIT = 100


def send_email_batch(messages: list[dict]) -> list[str]:
    """One Resend batch call → provider ids aligned with the input. Patched in tests."""
    resend.api_key = settings.RESEND_API_KEY
    response = resend.Batch.send(messages)
    data = response["data"] if isinstance(response, dict) else response.data
    return [item["id"] for item in data]


def email_channels(invitation: Invitation) -> list[ContactChannel]:
    """Resolve where an envelope's email goes (§2.3): the contact's preferred/first
    active email — or, for a household, each member's, deduped by address (both
    parents get the same link)."""
    if invitation.contact_id:
        contacts = [invitation.contact]
    else:
        contacts = list(invitation.household.members.all())

    channels, seen = [], set()
    for contact in contacts:
        emails = [
            ch
            for ch in contact.channels.all()
            if ch.kind == ContactChannel.Kind.EMAIL
            and ch.status == ContactChannel.Status.ACTIVE
            and ch.value
        ]
        best = next((ch for ch in emails if ch.is_preferred), emails[0] if emails else None)
        if best and best.value.lower() not in seen:
            seen.add(best.value.lower())
            channels.append(best)
    return channels


def dispatch_email(invitations, kind: str, base_url: str) -> dict:
    """Send `kind` to each invitation's resolved email address(es), synchronously.

    Creates one Delivery per address (audit, §5), sends via the batch endpoint, marks
    each SENT/FAILED, and advances invitation state through the monotonic ladder.
    Returns {"sent": n, "failed": n, "skipped": n} for the review screen's ✓/✗.
    """
    deliveries: list[tuple[Delivery, Invitation]] = []
    messages: list[dict] = []
    skipped = 0

    for invitation in invitations:
        channels = email_channels(invitation)
        if not channels:
            skipped += 1
            continue
        message = build_message(kind, invitation, base_url + invitation.rsvp_path)
        for channel in channels:
            delivery = Delivery.objects.create(
                invitation=invitation,
                channel=channel,
                kind=ContactChannel.Kind.EMAIL,
                address_used=channel.value,
                status=Delivery.Status.QUEUED,
            )
            messages.append(
                {
                    "from": settings.EMAIL_FROM,
                    "to": [channel.value],
                    "reply_to": settings.EMAIL_REPLY_TO or None,
                    "subject": message["subject"],
                    "text": message["text"],
                    "html": message["html"],
                }
            )
            deliveries.append((delivery, invitation))

    sent = failed = 0
    now = timezone.now()
    for start in range(0, len(messages), RESEND_BATCH_LIMIT):
        batch = deliveries[start : start + RESEND_BATCH_LIMIT]
        try:
            ids = send_email_batch(messages[start : start + RESEND_BATCH_LIMIT])
        except Exception as exc:  # provider/network error — fail this chunk, keep audit
            for delivery, _ in batch:
                delivery.status = Delivery.Status.FAILED
                delivery.error = str(exc)[:500]
                delivery.save(update_fields=["status", "error", "updated_at"])
            failed += len(batch)
            continue
        for (delivery, invitation), provider_id in zip(batch, ids, strict=False):
            delivery.status = Delivery.Status.SENT
            delivery.provider_message_id = provider_id or ""
            delivery.sent_at = now
            delivery.save(update_fields=["status", "provider_message_id", "sent_at", "updated_at"])
            invitation.advance_state(Invitation.State.SENT)
            sent += 1

    return {"sent": sent, "failed": failed, "skipped": skipped}


# --------------------------------------------------------------------------- #
#  Target selection for the send/notify actions (§2.3/§2.4)
# --------------------------------------------------------------------------- #
S = Invitation.State
NON_RESPONDER_STATES = (S.SENT, S.SHARED, S.OPENED)  # went out, no answer (bounced ≠ nudge)
NOTIFIED_STATES = (S.SENT, S.SHARED, S.OPENED, S.RESPONDED, S.BOUNCED)  # ever reached


def send_targets(event: Event) -> dict:
    """The review-screen breakdown: who gets what, before anything is sent."""
    invitations = list(
        event.invitations.select_related("contact", "household").prefetch_related(
            "contact__channels", "household__members__channels"
        )
    )
    by_state = lambda *states: [i for i in invitations if i.state in states]  # noqa: E731

    pending = by_state(S.PENDING, S.QUEUED)
    return {
        "pending_with_email": [i for i in pending if email_channels(i)],
        "pending_no_email": [i for i in pending if not email_channels(i)],
        "retryable": [
            i
            for i in invitations
            if i.state == S.BOUNCED
            or (
                i.state == S.PENDING and i.deliveries.filter(status=Delivery.Status.FAILED).exists()
            )
        ],
        "non_responders": [i for i in by_state(*NON_RESPONDER_STATES) if email_channels(i)],
        "notified": by_state(*NOTIFIED_STATES),
    }
