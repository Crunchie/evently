import base64
import binascii
import hashlib
import hmac
import json
import time

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Prefetch
from django.http import HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from .channels import dispatch_email, send_targets
from .ics import event_ics, google_calendar_url
from .models import Delivery, Event, Invitation, InvitationAttendee, RsvpEvent

RSVP_CHOICES = {
    InvitationAttendee.Rsvp.GOING,
    InvitationAttendee.Rsvp.MAYBE,
    InvitationAttendee.Rsvp.CANT,
}
MAX_PLUS_ONES = 9  # absolute ceiling when the event sets no cap
MAX_NOTE_LEN = 500


def healthz(request):
    """Liveness probe used by the Docker healthcheck (§9)."""
    return JsonResponse({"status": "ok"})


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
        "plus_cap": event.plus_ones_cap or MAX_PLUS_ONES,
        "going_names": going_names,
        "google_url": google_calendar_url(event),
    }
    return _guest_render(request, "core/rsvp.html", context)


def _apply_rsvp(request, invitation: Invitation) -> None:
    """Write the guest's answers: attendee statuses + history, envelope note/plus-ones.

    Status changes append to rsvp_events (actor=guest, §5); a note-only edit updates the
    denormalized latest_note without a history row.
    """
    event = invitation.event
    now = timezone.now()
    note = request.POST.get("note", "").strip()[:MAX_NOTE_LEN]

    for attendee in invitation.attendees.all():
        new_status = request.POST.get(f"status_{attendee.pk}", "")
        if new_status in RSVP_CHOICES and new_status != attendee.rsvp_status:
            attendee.rsvp_status = new_status
            attendee.responded_at = now
            attendee.save(update_fields=["rsvp_status", "responded_at", "updated_at"])
            RsvpEvent.objects.create(
                attendee=attendee, status=new_status, note=note, actor=RsvpEvent.Actor.GUEST
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


# --------------------------------------------------------------------------- #
#  Organizer side — basic per-event dashboard (§2.6). Lives under /admin so the
#  single Cloudflare Access path rule covers it (CLOUDFLARE_SETUP.md).
# --------------------------------------------------------------------------- #
@staff_member_required
def event_dashboard(request, pk):
    event = get_object_or_404(Event, pk=pk)
    invitations = list(
        event.invitations.select_related("contact", "household")
        .prefetch_related(
            Prefetch(
                "attendees",
                queryset=InvitationAttendee.objects.select_related("contact").order_by("id"),
            )
        )
        .order_by("id")
    )
    for inv in invitations:
        inv.rsvp_url = request.build_absolute_uri(inv.rsvp_path)

    attendee_qs = InvitationAttendee.objects.filter(invitation__event=event)
    by_status = {
        row["rsvp_status"]: row["n"]
        for row in attendee_qs.values("rsvp_status").annotate(n=Count("id"))
    }
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
        "result": {k: request.GET.get(k) for k in ("did", "sent", "failed", "skipped")}
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
        else:
            return HttpResponseForbidden("Unknown action")
        dash = reverse("event-dashboard", args=[event.pk])
        return redirect(
            f"{dash}?did={action}&sent={result['sent']}"
            f"&failed={result['failed']}&skipped={result['skipped']}"
        )

    return render(request, "core/send.html", {"event": event, "targets": targets})


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
