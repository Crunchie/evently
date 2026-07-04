import base64
import binascii
import hashlib
import hmac
import json
import time
from datetime import timedelta

import phonenumbers
from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.staticfiles import finders
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Count, Max, Prefetch
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from .channels import (
    assisted_channels,
    dispatch_email,
    email_channels,
    send_targets,
    shared_channel_pairs,
    wa_link,
)
from .ics import event_ics, google_calendar_url
from .messaging import share_payload
from .models import (
    ContactChannel,
    Delivery,
    Event,
    Invitation,
    InvitationAttendee,
    RsvpEvent,
    make_token,
)

RSVP_CHOICES = {
    InvitationAttendee.Rsvp.GOING,
    InvitationAttendee.Rsvp.MAYBE,
    InvitationAttendee.Rsvp.CANT,
}
# The organizer override (§2.3) can also take an answer *back* to no-reply.
ORGANIZER_RSVP_CHOICES = RSVP_CHOICES | {InvitationAttendee.Rsvp.NO_REPLY}
MAX_PLUS_ONES = 9  # absolute ceiling when the event sets no cap
MAX_NOTE_LEN = 500


def healthz(request):
    """Liveness probe used by the Docker healthcheck (§9)."""
    return JsonResponse({"status": "ok"})


def service_worker(request):
    """The organizer PWA's service worker (§7). A worker's scope can't exceed its
    script's path, so it's served under /admin/ (not /static/) — which also means
    Cloudflare Access gates it at the edge like every other organizer request."""
    source = finders.find("core/sw.js")
    with open(source, encoding="utf-8") as fh:
        body = fh.read()
    response = HttpResponse(body, content_type="application/javascript")
    response["Cache-Control"] = "no-cache"  # browsers re-check for SW updates
    return response


# --------------------------------------------------------------------------- #
#  Guest side — the RSVP page (§2.5). Capability URL, no login, ever.
# --------------------------------------------------------------------------- #
def _guest_render(request, template, context=None, status=200):
    response = render(request, template, context or {}, status=status)
    # §8: capability tokens live in the URL — never leak them via Referer.
    response["Referrer-Policy"] = "no-referrer"
    response["X-Robots-Tag"] = "noindex"
    return response


def _get_invitation(token: str) -> Invitation:
    return get_object_or_404(
        Invitation.objects.select_related("event", "contact", "household").prefetch_related(
            Prefetch(
                "attendees",
                queryset=InvitationAttendee.objects.select_related("contact").order_by("id"),
            )
        ),
        token=token,
    )


@require_http_methods(["GET", "POST"])
def rsvp_page(request, token):
    invitation = _get_invitation(token)
    event = invitation.event

    if invitation.state == Invitation.State.REVOKED:
        # Soft dead-end (§2.5): no event details leaked, deliberately vague.
        return _guest_render(request, "core/rsvp_unavailable.html", status=410)

    # First sight of the link is the real delivery signal (§2.3).
    if invitation.opened_at is None:
        invitation.opened_at = timezone.now()
        invitation.save(update_fields=["opened_at", "updated_at"])
    invitation.advance_state(Invitation.State.OPENED)

    cancelled = event.status == Event.Status.CANCELLED
    can_rsvp = event.status == Event.Status.ACTIVE and not event.is_past

    if request.method == "POST":
        if not can_rsvp:
            resp = HttpResponseForbidden("RSVPs are closed for this event.")
            resp["Referrer-Policy"] = "no-referrer"
            return resp
        _apply_rsvp(request, invitation)
        return redirect(f"{invitation.rsvp_path}?saved=1")

    attendees = list(invitation.attendees.all())
    going_names = []
    if event.show_guest_list:
        going_names = [
            a.contact.greeting_name
            for a in InvitationAttendee.objects.filter(
                invitation__event=event, rsvp_status=InvitationAttendee.Rsvp.GOING
            ).select_related("contact")
        ]

    context = {
        "invitation": invitation,
        "event": event,
        "attendees": attendees,
        "household": invitation.household_id is not None,
        "cancelled": cancelled,
        "past": event.is_past,
        "can_rsvp": can_rsvp,
        "saved": request.GET.get("saved") == "1",
        "channel_requested": request.GET.get("channel_requested") == "1",
        "channel_error": request.GET.get("channel_error") == "1",
        "channel_pending": invitation.channel_requests.filter(
            status=ContactChannel.Status.PROPOSED
        ).exists(),
        "plus_cap": event.plus_ones_cap or MAX_PLUS_ONES,
        "going_names": going_names,
        "google_url": google_calendar_url(event),
    }
    return _guest_render(request, "core/rsvp.html", context)


def _apply_rsvp(
    request,
    invitation: Invitation,
    *,
    actor: str = RsvpEvent.Actor.GUEST,
    actor_user=None,
    allowed: set = RSVP_CHOICES,
) -> None:
    """Write RSVP answers: attendee statuses + history, envelope note/plus-ones.

    Shared by the guest page and the organizer override (§2.3) — the same form
    fields, different actor recorded in the append-only history (§5). A note-only
    edit updates the denormalized latest_note without a history row.
    """
    event = invitation.event
    now = timezone.now()
    note = request.POST.get("note", "").strip()[:MAX_NOTE_LEN]

    for attendee in invitation.attendees.all():
        new_status = request.POST.get(f"status_{attendee.pk}", "")
        if new_status in allowed and new_status != attendee.rsvp_status:
            attendee.rsvp_status = new_status
            attendee.responded_at = now if new_status != InvitationAttendee.Rsvp.NO_REPLY else None
            attendee.save(update_fields=["rsvp_status", "responded_at", "updated_at"])
            RsvpEvent.objects.create(
                attendee=attendee,
                status=new_status,
                note=note,
                actor=actor,
                actor_user=actor_user,
            )

    if event.allow_plus_ones:
        try:
            requested = int(request.POST.get("plus_ones", invitation.plus_ones))
        except (TypeError, ValueError):
            requested = invitation.plus_ones
        invitation.plus_ones = max(0, min(requested, event.plus_ones_cap or MAX_PLUS_ONES))

    if note != invitation.latest_note:
        invitation.latest_note = note
        invitation.latest_note_at = now
    invitation.save(update_fields=["plus_ones", "latest_note", "latest_note_at", "updated_at"])

    if invitation.attendees.exclude(rsvp_status=InvitationAttendee.Rsvp.NO_REPLY).exists():
        invitation.advance_state(Invitation.State.RESPONDED)


def rsvp_ics(request, token):
    invitation = _get_invitation(token)
    if invitation.state == Invitation.State.REVOKED:
        return _guest_render(request, "core/rsvp_unavailable.html", status=410)
    body = event_ics(invitation.event, url=request.build_absolute_uri(invitation.rsvp_path))
    response = HttpResponse(body, content_type="text/calendar; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="event.ics"'
    response["Referrer-Policy"] = "no-referrer"
    return response


@require_POST
def rsvp_channel_request(request, token):
    """Guest asks to be reached differently (§2.5): creates a PROPOSED channel that
    sits in the dashboard approval queue — the organizer's review is the gate (§8).
    No login: the capability link itself is the authentication."""
    invitation = _get_invitation(token)
    if invitation.state == Invitation.State.REVOKED:
        return _guest_render(request, "core/rsvp_unavailable.html", status=410)

    kind = request.POST.get("kind", "")
    value = request.POST.get("value", "").strip()[:254]
    if kind not in (
        ContactChannel.Kind.EMAIL,
        ContactChannel.Kind.WHATSAPP,
        ContactChannel.Kind.MESSENGER,
    ):
        return redirect(f"{invitation.rsvp_path}?channel_error=1")

    # Whose channel: the single contact, or the chosen household member.
    if invitation.contact_id:
        contact = invitation.contact
    else:
        contact = invitation.household.members.filter(pk=request.POST.get("member")).first()
        if contact is None:
            return redirect(f"{invitation.rsvp_path}?channel_error=1")

    if kind == ContactChannel.Kind.EMAIL:
        try:
            validate_email(value)
        except ValidationError:
            return redirect(f"{invitation.rsvp_path}?channel_error=1")
    elif kind == ContactChannel.Kind.WHATSAPP:
        try:
            parsed = phonenumbers.parse(value, settings.PHONE_REGION)
        except phonenumbers.NumberParseException:
            return redirect(f"{invitation.rsvp_path}?channel_error=1")
        if not phonenumbers.is_valid_number(parsed):
            return redirect(f"{invitation.rsvp_path}?channel_error=1")
        value = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    else:  # Messenger needs no address (§6)
        value = ""

    with transaction.atomic():
        # One pending request per person: a newer ask replaces the older one.
        contact.channels.filter(
            status=ContactChannel.Status.PROPOSED, source=ContactChannel.Source.GUEST
        ).delete()
        ContactChannel.objects.create(
            contact=contact,
            kind=kind,
            value=value,
            status=ContactChannel.Status.PROPOSED,
            source=ContactChannel.Source.GUEST,
            requested_via=invitation,
        )
    return redirect(f"{invitation.rsvp_path}?channel_requested=1")


# --------------------------------------------------------------------------- #
#  Organizer side — basic per-event dashboard (§2.6). Lives under /admin so the
#  single Cloudflare Access path rule covers it (CLOUDFLARE_SETUP.md).
# --------------------------------------------------------------------------- #
REMINDER_WINDOW_HOURS = 48  # how close the event must be before the prompt appears


@staff_member_required
def event_dashboard(request, pk):
    event = get_object_or_404(Event, pk=pk)
    invitations = list(
        event.invitations.select_related("contact", "household")
        .prefetch_related(
            Prefetch(
                "attendees",
                queryset=InvitationAttendee.objects.select_related("contact").order_by("id"),
            ),
            "contact__channels",
            "household__members__channels",
        )
        .annotate(last_contacted=Max("deliveries__sent_at"))
        .order_by("id")
    )
    for inv in invitations:
        inv.rsvp_url = request.build_absolute_uri(inv.rsvp_path)
        inv.route_email = bool(email_channels(inv))
        inv.route_assisted = bool(assisted_channels(inv))

    attendee_qs = InvitationAttendee.objects.filter(invitation__event=event)
    by_status = {
        row["rsvp_status"]: row["n"]
        for row in attendee_qs.values("rsvp_status").annotate(n=Count("id"))
    }

    # Anti-spam context + Phase 6 streams (§2.4/§2.6).
    until_start = event.starts_at - timezone.now()
    reminder_due = event.status == Event.Status.ACTIVE and timedelta() < until_start <= timedelta(
        hours=REMINDER_WINDOW_HOURS
    )
    context = {
        "event": event,
        "invitations": invitations,
        "going": by_status.get(InvitationAttendee.Rsvp.GOING, 0),
        "maybe": by_status.get(InvitationAttendee.Rsvp.MAYBE, 0),
        "cant": by_status.get(InvitationAttendee.Rsvp.CANT, 0),
        "no_reply": by_status.get(InvitationAttendee.Rsvp.NO_REPLY, 0),
        "invited": attendee_qs.count(),
        "opened": event.invitations.filter(opened_at__isnull=False).count(),
        "expected": event.expected_headcount,
        "pending_channels": ContactChannel.objects.filter(status=ContactChannel.Status.PROPOSED)
        .select_related("contact", "requested_via__event")
        .order_by("created_at"),
        "notes": [i for i in invitations if i.latest_note],
        "history": RsvpEvent.objects.filter(attendee__invitation__event=event).select_related(
            "attendee__contact", "actor_user"
        )[:30],
        "reminder_due": reminder_due,
        "result": {k: request.GET.get(k) for k in ("did", "sent", "failed", "skipped", "msg")}
        if request.GET.get("did")
        else None,
    }
    return render(request, "core/dashboard.html", context)


@staff_member_required
@require_http_methods(["GET", "POST"])
def event_send(request, pk):
    """Send review screen (§2.3) + the notify actions (§2.4). Sends are synchronous
    (§9): the redirect back to the dashboard carries per-guest ✓/✗ counts."""
    event = get_object_or_404(Event, pk=pk)
    targets = send_targets(event)

    if request.method == "POST":
        action = request.POST.get("action", "")
        base_url = request.build_absolute_uri("/").rstrip("/")
        if action == "invites":
            result = dispatch_email(targets["pending_with_email"], "invite", base_url)
            if result["sent"] and event.status == Event.Status.DRAFT:
                event.status = Event.Status.ACTIVE  # first send flips draft → active (§2.1)
                event.save(update_fields=["status", "updated_at"])
        elif action == "retry":
            result = dispatch_email(targets["retryable"], "invite", base_url)
        elif action == "nudge":
            result = dispatch_email(targets["non_responders"], "nudge", base_url)
        elif action == "update":
            result = dispatch_email(targets["notified"], "update", base_url)
        elif action == "cancel":
            if event.status != Event.Status.CANCELLED:
                event.status = Event.Status.CANCELLED
                event.save(update_fields=["status", "updated_at"])
            result = dispatch_email(targets["notified"], "cancellation", base_url)
        elif action == "reminder":
            result = dispatch_email(targets["reminder_email"], "reminder", base_url)
        else:
            return HttpResponseForbidden("Unknown action")
        dash = reverse("event-dashboard", args=[event.pk])
        return redirect(
            f"{dash}?did={action}&sent={result['sent']}"
            f"&failed={result['failed']}&skipped={result['skipped']}"
        )

    return render(request, "core/send.html", {"event": event, "targets": targets})


# --------------------------------------------------------------------------- #
#  Send queue — assisted channels (§6, Phase 5). One share at a time: the
#  organizer taps through Messenger/WhatsApp invitees; each share is recorded as
#  an optimistic SHARED delivery (the guest's link click is the real signal).
# --------------------------------------------------------------------------- #
QUEUE_TARGET_KEY = {
    "invite": "pending_assisted",
    "nudge": "non_responders_assisted",
    "update": "notified_assisted",
    "cancellation": "notified_assisted",
    "reminder": "reminder_assisted",
}


def _queue_items(event: Event, kind: str) -> list[tuple[Invitation, ContactChannel]]:
    """The flattened queue: one card per (envelope, assisted channel) — a household
    with two WhatsApp parents is two taps carrying the same link. For invites,
    already-shared copies drop out; notify kinds re-share deliberately (§2.4)."""
    targets = send_targets(event)
    items = [
        (invitation, channel)
        for invitation in targets[QUEUE_TARGET_KEY[kind]]
        for channel in assisted_channels(invitation)
    ]
    if kind == "invite":
        shared = shared_channel_pairs(event)
        items = [(inv, ch) for inv, ch in items if (inv.pk, ch.pk) not in shared]
    return items


@staff_member_required
@require_http_methods(["GET", "POST"])
def event_queue(request, pk):
    event = get_object_or_404(Event, pk=pk)
    params = request.POST if request.method == "POST" else request.GET
    kind = params.get("kind", "invite")
    if kind not in QUEUE_TARGET_KEY:
        return HttpResponseForbidden("Unknown queue kind")
    try:
        n = max(0, int(params.get("n", 0)))
    except ValueError:
        n = 0

    if request.method == "POST":
        action = request.POST.get("action", "")
        invitation = get_object_or_404(Invitation, pk=request.POST.get("invitation"), event=event)
        channel = get_object_or_404(ContactChannel, pk=request.POST.get("channel"))
        if action == "shared":
            Delivery.objects.create(
                invitation=invitation,
                channel=channel,
                kind=channel.kind,
                address_used=channel.value,
                status=Delivery.Status.SHARED,
                sent_at=timezone.now(),
            )
            invitation.advance_state(Invitation.State.SHARED)
            # Invite-kind items leave the queue once shared (state moved past
            # PENDING) — the list shifts under n. Other kinds stay put: step over.
            items = _queue_items(event, kind)
            if n < len(items) and (items[n][0].pk, items[n][1].pk) == (invitation.pk, channel.pk):
                n += 1
        elif action == "skip":
            n += 1
        else:
            return HttpResponseForbidden("Unknown action")
        return redirect(f"{reverse('event-queue', args=[event.pk])}?kind={kind}&n={n}")

    items = _queue_items(event, kind)
    base_url = request.build_absolute_uri("/").rstrip("/")
    item = None
    if n < len(items):
        invitation, channel = items[n]
        url = base_url + invitation.rsvp_path
        payload = share_payload(kind, invitation, url)
        item = {
            "invitation": invitation,
            "channel": channel,
            "payload": payload,
            "wa_url": (
                wa_link(channel.value, payload["text"])
                if channel.kind == ContactChannel.Kind.WHATSAPP
                else None
            ),
            "last_contacted": invitation.deliveries.aggregate(t=Max("sent_at"))["t"],
        }
    context = {"event": event, "kind": kind, "n": n, "total": len(items), "item": item}
    return render(request, "core/queue.html", context)


# --------------------------------------------------------------------------- #
#  Per-guest row actions + organizer override (§2.3) and the channel-change
#  approval queue (§2.5) — all POST-only, all staff-only, all land back on the
#  dashboard with a banner.
# --------------------------------------------------------------------------- #
def _dash(event_pk, did, **params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return redirect(f"{reverse('event-dashboard', args=[event_pk])}?did={did}&{query}")


@staff_member_required
@require_POST
def invitation_action(request, pk):
    invitation = get_object_or_404(Invitation.objects.select_related("event"), pk=pk)
    action = request.POST.get("action", "")
    base_url = request.build_absolute_uri("/").rstrip("/")

    if action == "revoke":
        invitation.advance_state(Invitation.State.REVOKED)
        return _dash(invitation.event_id, action, msg="Invitation revoked — link now dead")
    if action == "regenerate":
        # New capability token: the old link is dead, the invitation lives on (§2.3).
        invitation.token = make_token()
        invitation.save(update_fields=["token", "updated_at"])
        return _dash(invitation.event_id, action, msg="New link generated — old one is dead")
    if action in ("resend", "nudge"):
        kind = "invite" if action == "resend" else "nudge"
        result = dispatch_email([invitation], kind, base_url)
        return _dash(invitation.event_id, action, **result)
    return HttpResponseForbidden("Unknown action")


@staff_member_required
@require_POST
def invitation_override(request, pk):
    """Set RSVP status / plus-ones / note on the guest's behalf (§2.3). One form
    covers every member of a household envelope; recorded as actor=organizer, and
    the guest can still change it later via their link — last write wins."""
    invitation = get_object_or_404(Invitation.objects.select_related("event"), pk=pk)
    _apply_rsvp(
        request,
        invitation,
        actor=RsvpEvent.Actor.ORGANIZER,
        actor_user=request.user,
        allowed=ORGANIZER_RSVP_CHOICES,
    )
    return _dash(invitation.event_id, "override", msg="RSVP updated on their behalf")


@staff_member_required
@require_POST
def channel_request_action(request, pk):
    """One-tap approve / reject for a guest-requested channel (§2.5). Approval makes
    it the contact's preferred channel — future sends follow it."""
    channel = get_object_or_404(ContactChannel, pk=pk, status=ContactChannel.Status.PROPOSED)
    action = request.POST.get("action", "")
    event_pk = request.POST.get("event", "")
    if action == "approve":
        with transaction.atomic():
            channel.contact.channels.filter(is_preferred=True).exclude(pk=channel.pk).update(
                is_preferred=False
            )
            channel.status = ContactChannel.Status.ACTIVE
            channel.is_preferred = True
            channel.save(update_fields=["status", "is_preferred", "updated_at"])
        return _dash(event_pk, action, msg=f"{channel.contact.name} → {channel.get_kind_display()}")
    if action == "reject":
        name = channel.contact.name
        channel.delete()
        return _dash(event_pk, action, msg=f"Request from {name} rejected")
    return HttpResponseForbidden("Unknown action")


# --------------------------------------------------------------------------- #
#  Resend bounce webhook (§9) — the only inbound HTTP besides the RSVP page (§8).
#  Signed by Resend using the Svix scheme; verified here, fail closed.
# --------------------------------------------------------------------------- #
SIGNATURE_TOLERANCE_SECONDS = 300


def _valid_webhook_signature(request) -> bool:
    secret = settings.RESEND_WEBHOOK_SECRET
    if not secret:
        return False  # unset secret = webhook disabled, never open
    msg_id = request.headers.get("svix-id", "")
    stamp = request.headers.get("svix-timestamp", "")
    signatures = request.headers.get("svix-signature", "")
    if not (msg_id and stamp and signatures):
        return False
    try:
        if abs(time.time() - int(stamp)) > SIGNATURE_TOLERANCE_SECONDS:
            return False  # replay protection
        raw = secret.split("_", 1)[1] if secret.startswith("whsec_") else secret
        key = base64.b64decode(raw)
    except (ValueError, IndexError, binascii.Error):
        return False
    signed = f"{msg_id}.{stamp}.".encode() + request.body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    return any(
        hmac.compare_digest(expected, candidate.split(",", 1)[1])
        for candidate in signatures.split()
        if "," in candidate
    )


@csrf_exempt
@require_POST
def resend_webhook(request):
    if not _valid_webhook_signature(request):
        return HttpResponseForbidden("invalid webhook signature")
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        return HttpResponse(status=400)

    if payload.get("type") in ("email.bounced", "email.complained"):
        data = payload.get("data") or {}
        provider_id = data.get("email_id") or data.get("id") or ""
        delivery = (
            Delivery.objects.select_related("invitation")
            .filter(provider_message_id=provider_id)
            .first()
        )
        if delivery:
            delivery.status = Delivery.Status.BOUNCED
            delivery.error = payload["type"]
            delivery.save(update_fields=["status", "error", "updated_at"])
            # Ladder rules apply: a bounce can't override an open/response (§2.3).
            delivery.invitation.advance_state(Invitation.State.BOUNCED)
    return JsonResponse({"ok": True})
