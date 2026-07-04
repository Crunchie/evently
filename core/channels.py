"""Outbound dispatcher: the email (Resend) channel + assisted channels (§4/§6/§9).

Channels come in two flavors: **automated** (the app calls an API — email) and
**assisted** (the app prepares a share payload and a human taps send — Messenger via
the share sheet, WhatsApp via a wa.me deep link). Every channel delivers the same
thing: the guest's unique RSVP link.

Routing (§2.2/§2.3): each covered person goes out on their **preferred** active
channel; without one, email beats WhatsApp beats Messenger (automated beats assisted,
and within assisted, direct targeting beats friend-picking). A household envelope may
route different members down different paths — email copies go in the batch, assisted
members appear in the send queue, all carrying the same link.

Sends are **synchronous in the request** (§9 — no cron, no queue): the view calls
`dispatch_email()` and gets per-delivery outcomes back immediately; assisted "sends"
are Delivery rows marked SHARED when the organizer invokes the share (optimistic —
the guest clicking the link is the real signal). `Delivery` rows are the audit
record; bounces arrive later via the signature-verified webhook (views).
"""

from urllib.parse import quote

import phonenumbers
import resend
from django.conf import settings
from django.utils import timezone

from .messaging import build_message
from .models import ContactChannel, Delivery, Event, Invitation

RESEND_BATCH_LIMIT = 100

Kind = ContactChannel.Kind
ASSISTED_KINDS = (Kind.WHATSAPP, Kind.MESSENGER)
# Fallback order when no preferred channel is set (or the preferred one is unusable).
ROUTE_ORDER = (Kind.EMAIL, Kind.WHATSAPP, Kind.MESSENGER)


def _usable(channel: ContactChannel) -> bool:
    """A channel an invite can actually go out on. Messenger needs no address (the
    share sheet targets the friend); everything else needs one. SMS/Telegram have no
    transport yet (§10 Phase 2) — never route to them."""
    if channel.status != ContactChannel.Status.ACTIVE:
        return False
    if channel.kind == Kind.MESSENGER:
        return True
    return channel.kind in ROUTE_ORDER and bool(channel.value)


def route_channel(contact) -> ContactChannel | None:
    """The channel this person's invite actually goes out on, or None (§2.2)."""
    usable = [ch for ch in contact.channels.all() if _usable(ch)]
    preferred = next((ch for ch in usable if ch.is_preferred), None)
    if preferred:
        return preferred
    for kind in ROUTE_ORDER:
        match = next((ch for ch in usable if ch.kind == kind), None)
        if match:
            return match
    return None


def _covered_contacts(invitation: Invitation) -> list:
    if invitation.contact_id:
        return [invitation.contact]
    return list(invitation.household.members.all())


def wa_link(phone: str, text: str) -> str | None:
    """`wa.me/<E.164>?text=` deep link (§6), or None when the number won't parse.
    Numbers are stored loosely; normalise via phonenumbers with the local default
    region so "021 555 0123" and "+64215550123" both work."""
    try:
        parsed = phonenumbers.parse(phone, settings.PHONE_REGION)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_valid_number(parsed):
        return None
    e164 = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    return f"https://wa.me/{e164.lstrip('+')}?text={quote(text)}"


def send_email_batch(messages: list[dict]) -> list[str]:
    """One Resend batch call → provider ids aligned with the input. Patched in tests."""
    resend.api_key = settings.RESEND_API_KEY
    response = resend.Batch.send(messages)
    data = response["data"] if isinstance(response, dict) else response.data
    return [item["id"] for item in data]


def email_channels(invitation: Invitation) -> list[ContactChannel]:
    """The envelope's email recipients (§2.3): each covered person **routed to
    email**, deduped by address (both parents get the same link). A member whose
    preferred channel is assisted is *not* emailed — they enter the send queue."""
    channels, seen = [], set()
    for contact in _covered_contacts(invitation):
        route = route_channel(contact)
        if route and route.kind == Kind.EMAIL and route.value.lower() not in seen:
            seen.add(route.value.lower())
            channels.append(route)
    return channels


def assisted_channels(invitation: Invitation) -> list[ContactChannel]:
    """The envelope's send-queue entries: each covered person routed to WhatsApp or
    Messenger. Deduped by address (shared phone → one wa.me tap) but *not* across
    Messenger members — the share sheet targets one friend at a time."""
    channels, seen = [], set()
    for contact in _covered_contacts(invitation):
        route = route_channel(contact)
        if route is None or route.kind not in ASSISTED_KINDS:
            continue
        key = (route.kind, route.value.lower()) if route.value else ("contact", contact.pk)
        if key not in seen:
            seen.add(key)
            channels.append(route)
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


def shared_channel_pairs(event: Event) -> set[tuple[int, int]]:
    """(invitation_id, channel_id) pairs already marked SHARED — a household stays
    in the invite queue until *each* assisted copy has gone out."""
    return set(
        Delivery.objects.filter(invitation__event=event, status=Delivery.Status.SHARED).values_list(
            "invitation_id", "channel_id"
        )
    )


def send_targets(event: Event) -> dict:
    """The review-screen breakdown: who gets what, before anything is sent. Each
    action has an email list (dispatched in-request) and an assisted list (walked
    through the send queue); a mixed-route household can appear in both."""
    invitations = list(
        event.invitations.select_related("contact", "household").prefetch_related(
            "contact__channels", "household__members__channels", "attendees"
        )
    )
    by_state = lambda *states: [i for i in invitations if i.state in states]  # noqa: E731

    shared_pairs = shared_channel_pairs(event)
    # One query, not one per invitation: which envelopes have a failed delivery.
    failed_ids = set(
        Delivery.objects.filter(invitation__event=event, status=Delivery.Status.FAILED).values_list(
            "invitation_id", flat=True
        )
    )

    def unshared_assisted(invitation):
        return [
            ch for ch in assisted_channels(invitation) if (invitation.pk, ch.pk) not in shared_pairs
        ]

    pending = by_state(S.PENDING, S.QUEUED)
    non_responders = by_state(*NON_RESPONDER_STATES)
    notified = by_state(*NOTIFIED_STATES)
    # Day-before reminder (§2.4): everyone with at least one Going/Maybe answer.
    reminder = [
        i
        for i in invitations
        if i.state != S.REVOKED
        and any(
            a.rsvp_status in ("going", "maybe")
            for a in i.attendees.all()  # prefetched
        )
    ]
    return {
        "pending_with_email": [i for i in pending if email_channels(i)],
        # Invite-queue targets: not-yet-sent envelopes, plus SHARED ones with an
        # assisted copy still to go (the two-WhatsApp-parents household).
        "pending_assisted": [
            i for i in by_state(S.PENDING, S.QUEUED, S.SHARED) if unshared_assisted(i)
        ],
        "pending_no_channel": [
            i for i in pending if not email_channels(i) and not assisted_channels(i)
        ],
        "retryable": [
            i
            for i in invitations
            if i.state == S.BOUNCED or (i.state == S.PENDING and i.pk in failed_ids)
        ],
        "non_responders": [i for i in non_responders if email_channels(i)],
        "non_responders_assisted": [i for i in non_responders if assisted_channels(i)],
        "notified": notified,
        "notified_assisted": [i for i in notified if assisted_channels(i)],
        "reminder_email": [i for i in reminder if email_channels(i)],
        "reminder_assisted": [i for i in reminder if assisted_channels(i)],
    }
