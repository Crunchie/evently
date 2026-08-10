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

**Nothing is sent as a side effect of anything (§2.3).** Every action that needs to
reach guests calls `enqueue()`, which writes `pending` Delivery rows and stops. Email
leaves when the organizer presses the button on the Messages screen
(`send_pending_emails()`, still synchronous in the request — §9, no cron); assisted
messages leave when a human shares them and says so (`mark_sent()`). The outbox is the
one place that knows what is owed and what has gone; bounces arrive later via the
signature-verified webhook (views).
"""

from urllib.parse import quote

import phonenumbers
import resend
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.utils import timezone

from . import messaging
from .models import ContactChannel, Delivery, Event, Invitation

RESEND_BATCH_LIMIT = 100

Kind = ContactChannel.Kind
ASSISTED_KINDS = (Kind.WHATSAPP, Kind.MESSENGER)
# Fallback order when no preferred channel is set (or the preferred one is unusable).
ROUTE_ORDER = (Kind.EMAIL, Kind.WHATSAPP, Kind.MESSENGER)
# Channel kinds an organizer or guest can actually enter/request (§2.5). Telegram is a
# schema value with no entry UI or transport yet (§10 Phase 2).
ENTRY_KINDS = (Kind.EMAIL, Kind.WHATSAPP, Kind.MESSENGER, Kind.SMS)


def validate_channel_value(kind: str, value: str) -> tuple[str, str | None]:
    """Normalise + validate a channel address for the organizer contact forms and the
    guest channel-change request (§2.5). Returns (normalised_value, error): a truthy
    error string means reject. Email is validated; WhatsApp/SMS are parsed to E.164 via
    the local default region so "021 555 0123" and "+64215550123" both work; Messenger
    needs no address (value forced blank)."""
    value = (value or "").strip()
    if kind == Kind.EMAIL:
        try:
            validate_email(value)
        except ValidationError:
            return value, "Enter a valid email address."
        return value, None
    if kind in (Kind.WHATSAPP, Kind.SMS):
        try:
            parsed = phonenumbers.parse(value, settings.PHONE_REGION)
        except phonenumbers.NumberParseException:
            return value, "Enter a valid phone number."
        if not phonenumbers.is_valid_number(parsed):
            return value, "Enter a valid phone number."
        return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164), None
    if kind == Kind.MESSENGER:
        return "", None
    return value, "Unknown channel type."


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


def _list_unsubscribe_headers() -> dict:
    """List-Unsubscribe pointing at the reply-to inbox, so a recipient can opt out and
    the organizer can suppress that address. Empty dict when no reply-to is configured
    (an unactionable header is worse than none)."""
    unsub = settings.EMAIL_REPLY_TO
    if not unsub:
        return {}
    return {"List-Unsubscribe": f"<mailto:{unsub}?subject=unsubscribe>"}


def send_email_batch(messages: list[dict]) -> list[str]:
    """One Resend batch call → provider ids aligned with the input. Patched in tests."""
    resend.api_key = settings.RESEND_API_KEY
    response = resend.Batch.send(messages)
    data = response["data"] if isinstance(response, dict) else response.data
    return [item["id"] for item in data]


def send_feedback_email(feedback) -> bool:
    """Best-effort: email the organizer a guest's feedback. Returns True if handed to the
    provider. Never raises — the Feedback row is already the durable record (§2.5); this
    notification is a bonus, so a missing key or provider hiccup is a silent no-op."""
    if not (settings.RESEND_API_KEY and settings.FEEDBACK_EMAIL and settings.EMAIL_FROM):
        return False
    event = feedback.event
    context = [f"Event: {event.title}" if event else "Event: (unknown)"]
    if feedback.reply_email:
        context.append(f"Reply to guest: {feedback.reply_email}")
    if feedback.page_path:
        context.append(f"Page: {feedback.page_path}")
    if feedback.user_agent:
        context.append(f"Browser: {feedback.user_agent}")
    text = feedback.message.strip() + "\n\n—\n" + "\n".join(context)
    subject = f"Feedback — {event.title}" if event else "Feedback on your invites"
    try:
        resend.api_key = settings.RESEND_API_KEY
        resend.Emails.send(
            {
                "from": settings.EMAIL_FROM,
                "to": [settings.FEEDBACK_EMAIL],
                # Let a hitting-reply land on the guest when they left an address.
                "reply_to": feedback.reply_email or settings.EMAIL_REPLY_TO or None,
                "subject": subject,
                "text": text,
            }
        )
        return True
    except Exception:  # provider/network/config error — the record already captured it
        return False


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


# --------------------------------------------------------------------------- #
#  The outbox: enqueue → send / mark sent (§2.3)
# --------------------------------------------------------------------------- #
def rsvp_url(invitation: Invitation, base_url: str) -> str:
    return base_url + invitation.rsvp_path


def enqueue(invitations, message_kind: str, base_url: str) -> dict:
    """Write `pending` outbox rows for `message_kind`. **Sends nothing.**

    One row per resolved channel — a household with two WhatsApp parents gets two, both
    carrying the same link. A guest with no usable channel gets a single `blocked` row
    rather than being silently dropped: the outbox is meant to be the complete list, and
    "nobody to send this to" is information the organizer needs.

    Idempotent (§9): a channel that already holds a pending row for this kind is left
    alone, so pressing "queue nudge" twice queues one nudge. Counted as `already_queued`
    so the banner can say so instead of pretending it queued something.
    """
    queued = blocked = already = 0
    for invitation in invitations:
        url = rsvp_url(invitation, base_url)
        subject, text = messaging.render(message_kind, invitation, url)
        targets = email_channels(invitation) + assisted_channels(invitation)

        if not targets:
            # No channel at all. One blocked row per (invitation, kind) — the partial
            # unique constraint enforces it; get_or_create keeps a re-queue quiet.
            _, created = Delivery.objects.get_or_create(
                invitation=invitation,
                message_kind=message_kind,
                status=Delivery.Status.BLOCKED,
                channel=None,
                defaults={"subject": subject, "body": text},
            )
            blocked += 1 if created else 0
            already += 0 if created else 1
            continue

        pending_channel_ids = set(
            Delivery.objects.filter(
                invitation=invitation,
                message_kind=message_kind,
                status=Delivery.Status.PENDING,
            ).values_list("channel_id", flat=True)
        )
        new_rows = 0
        for channel in targets:
            if channel.pk in pending_channel_ids:
                already += 1
                continue
            Delivery.objects.create(
                invitation=invitation,
                channel=channel,
                channel_kind=channel.kind,
                message_kind=message_kind,
                address_used=channel.value,
                status=Delivery.Status.PENDING,
                subject=subject,
                body=text,
            )
            new_rows += 1
        queued += new_rows
        if new_rows:
            invitation.advance_state(Invitation.State.QUEUED)
    return {"queued": queued, "blocked": blocked, "already_queued": already}


def _body_for_send(delivery: Delivery, base_url: str) -> tuple[str, str, str]:
    """(subject, text, url) for a row about to go out.

    An **edited** row is sent verbatim — those are the organizer's words. An unedited row
    is re-rendered from current event data and its snapshot refreshed, so fixing a typo in
    the event reaches everyone whose message hasn't left yet, without queueing a second
    one (§12.3). This is the only place that rule lives.
    """
    url = rsvp_url(delivery.invitation, base_url)
    if delivery.is_edited:
        return delivery.subject, delivery.body, url
    subject, text = messaging.render(delivery.message_kind, delivery.invitation, url)
    if (subject, text) != (delivery.subject, delivery.body):
        delivery.subject, delivery.body = subject, text
        delivery.save(update_fields=["subject", "body", "updated_at"])
    return subject, text, url


def pending_email(event: Event):
    """The rows the Send button will send, in the order the screen lists them."""
    return (
        Delivery.objects.filter(
            invitation__event=event,
            status=Delivery.Status.PENDING,
            channel_kind=Kind.EMAIL,
        )
        .select_related("invitation__contact", "invitation__household", "invitation__event")
        .order_by("id")
    )


def send_pending_emails(event, base_url: str, *, actor=None) -> dict:
    """Send every pending email row for this event, whatever the message kind.

    No kind or subset argument by design (§7.2): holding one message back is cancelling
    its row first, so the button always means "send what the list shows". Batched through
    Resend exactly as the old synchronous sender was, marking each row sent/failed and
    advancing the envelope. Returns {"sent": n, "failed": n}.
    """
    deliveries = list(pending_email(event))
    messages = []
    for delivery in deliveries:
        subject, text, url = _body_for_send(delivery, base_url)
        message = messaging.email_payload(subject, text, url, delivery.message_kind)
        messages.append(
            {
                "from": settings.EMAIL_FROM,
                "to": [delivery.address_used],
                "reply_to": settings.EMAIL_REPLY_TO or None,
                "subject": message["subject"],
                "text": message["text"],
                "html": message["html"],
                # List-Unsubscribe is a deliverability trust signal (Gmail/Yahoo bulk
                # guidance). mailto only — we have no one-click POST endpoint, so we
                # deliberately omit List-Unsubscribe-Post rather than claim it falsely.
                "headers": _list_unsubscribe_headers(),
            }
        )

    sent = failed = 0
    now = timezone.now()
    for start in range(0, len(messages), RESEND_BATCH_LIMIT):
        batch = deliveries[start : start + RESEND_BATCH_LIMIT]
        try:
            ids = send_email_batch(messages[start : start + RESEND_BATCH_LIMIT])
        except Exception as exc:  # provider/network error — fail this chunk, keep audit
            for delivery in batch:
                _fail(delivery, str(exc)[:500])
            failed += len(batch)
            continue
        for delivery, provider_id in zip(batch, ids, strict=False):
            delivery.status = Delivery.Status.SENT
            delivery.provider_message_id = provider_id or ""
            delivery.sent_at = now
            delivery.sent_by = actor if actor and actor.is_authenticated else None
            delivery.save(
                update_fields=["status", "provider_message_id", "sent_at", "sent_by", "updated_at"]
            )
            delivery.invitation.advance_state(Invitation.State.SENT)
            sent += 1
        # Provider returned fewer ids than messages: the unmatched tail would otherwise
        # sit pending forever, invisible to the ✓/✗ counts.
        for delivery in batch[len(ids) :]:
            _fail(delivery, "provider returned no message id")
            failed += 1

    return {"sent": sent, "failed": failed}


def _fail(delivery: Delivery, error: str) -> None:
    delivery.status = Delivery.Status.FAILED
    delivery.error = error
    delivery.save(update_fields=["status", "error", "updated_at"])


def mark_sent(delivery: Delivery, actor=None) -> None:
    """Record that a message went out by hand (§7.3).

    The organizer's word is the signal — the app never marks an assisted message sent on
    their behalf, because opening a share sheet is not evidence that anything was sent.
    Advances the envelope to SHARED for an assisted channel, SENT otherwise (an email row
    the organizer handled from their own inbox, or a blocked row they covered in person).
    """
    delivery.status = Delivery.Status.SENT
    delivery.sent_at = timezone.now()
    delivery.sent_by = actor if actor and actor.is_authenticated else None
    delivery.deferred_at = None
    delivery.save(update_fields=["status", "sent_at", "sent_by", "deferred_at", "updated_at"])
    state = (
        Invitation.State.SHARED
        if delivery.channel_kind in ASSISTED_KINDS
        else Invitation.State.SENT
    )
    delivery.invitation.advance_state(state)


def cancel_pending(event: Event, *, exclude_kind: str | None = None) -> int:
    """Drop every message still waiting to go out for this event.

    Used when cancelling an event: sending an invite *after* the cancellation notice is
    the worst thing this system could do, so the queue is cleared before the cancellation
    is queued (§9). Returns how many were dropped.
    """
    rows = Delivery.objects.filter(invitation__event=event, status__in=Delivery.OUTSTANDING)
    if exclude_kind:
        rows = rows.exclude(message_kind=exclude_kind)
    return rows.update(status=Delivery.Status.CANCELLED, updated_at=timezone.now())


def cancel_pending_for(invitation: Invitation) -> int:
    """Same, for one envelope — uninviting someone shouldn't leave their invite queued."""
    return Delivery.objects.filter(invitation=invitation, status__in=Delivery.OUTSTANDING).update(
        status=Delivery.Status.CANCELLED, updated_at=timezone.now()
    )


# --------------------------------------------------------------------------- #
#  Target selection for the send/notify actions (§2.3/§2.4)
# --------------------------------------------------------------------------- #
S = Invitation.State
NON_RESPONDER_STATES = (S.SENT, S.SHARED, S.OPENED)  # went out, no answer (bounced ≠ nudge)
NOTIFIED_STATES = (S.SENT, S.SHARED, S.OPENED, S.RESPONDED, S.BOUNCED)  # ever reached


def send_targets(event: Event) -> dict:
    """Who the audience is for each action — **not** what's outstanding.

    Since the outbox stores what's owed, "still to do" is a query on Delivery, not a
    computation here (§4.4). What survives is the genuinely per-action question: who
    should receive a nudge, an update, a reminder? Each value is a list of envelopes to
    hand to `enqueue()`, which then works out their channels.
    """
    invitations = list(
        event.invitations.select_related("contact", "household").prefetch_related(
            "contact__channels", "household__members__channels", "attendees"
        )
    )
    by_state = lambda *states: [i for i in invitations if i.state in states]  # noqa: E731

    # Envelopes with a send that didn't land. A retry re-queues them, so a bounced
    # address gets another go once the organizer has fixed or switched the channel.
    retryable_ids = set(
        Delivery.objects.filter(invitation__event=event, status__in=Delivery.RETRYABLE).values_list(
            "invitation_id", flat=True
        )
    )

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
        "invites": by_state(S.PENDING, S.QUEUED),
        "retryable": [i for i in invitations if i.pk in retryable_ids],
        "non_responders": by_state(*NON_RESPONDER_STATES),
        "notified": by_state(*NOTIFIED_STATES),
        "reminder": reminder,
    }
