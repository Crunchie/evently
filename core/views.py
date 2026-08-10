import base64
import binascii
import hashlib
import hmac
import json
import time
from datetime import timedelta
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.staticfiles import finders
from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q
from django.http import Http404, HttpResponse, HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods, require_POST

from .channels import (
    ASSISTED_KINDS,
    ENTRY_KINDS,
    assisted_channels,
    cancel_pending,
    cancel_pending_for,
    email_channels,
    enqueue,
    mark_sent,
    rsvp_url,
    send_feedback_email,
    send_pending_emails,
    send_targets,
    validate_channel_value,
    wa_link,
)
from .ics import event_ics, google_calendar_url, google_maps_embed_url
from .messaging import render as render_message
from .messaging import share_payload_from
from .models import (
    Contact,
    ContactChannel,
    Delivery,
    Event,
    Feedback,
    Household,
    Invitation,
    InvitationAttendee,
    Poll,
    PollOption,
    PollVote,
    RsvpEvent,
    Tag,
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
MAX_POLL_OPTIONS = 20  # per poll, organizer + guest-added combined
MAX_POLL_OPTION_LEN = 100  # matches PollOption.text
MAX_POLL_QUESTION_LEN = 200  # matches Poll.question
MAX_FEEDBACK_LEN = 2000  # a bug report, not an essay


def _posted_pk(request, field: str) -> int:
    """An integer pk out of POST data. A non-numeric value must 404 like a missing
    row, not 500 — get_object_or_404 lets the int cast's ValueError escape."""
    try:
        return int(request.POST.get(field, ""))
    except (TypeError, ValueError):
        raise Http404(f"invalid {field}")


def healthz(request):
    """Liveness probe used by the Docker healthcheck (§9)."""
    return JsonResponse({"status": "ok"})


def security_txt(request):
    """security.txt (RFC 9116) at /.well-known/security.txt — where researchers
    look to report a vulnerability. Expires is a required field; we compute it a
    year out on each request so the file never lapses without a redeploy."""
    contact = "security@samandmonevents.party"
    expires = (timezone.now() + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = f"Contact: mailto:{contact}\nExpires: {expires}\n"
    return HttpResponse(body, content_type="text/plain; charset=utf-8")


def landing(request):
    """The bare apex "/". The app has no public front door — guests arrive on their
    own capability link (/i/<token>) and organizers on /admin. So this is a deliberate
    dead-end: a styled "nothing to see here" rather than a bare 404 for anyone (or any
    bot) who hits the root."""
    return render(request, "core/landing.html")


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


def version(request):
    """Build stamp for the organizer PWA's auto-refresh (§7). app.js checks this against the
    stamp the open page loaded with; when a new image is deployed the value changes and the
    PWA reloads itself. Tiny and never cached so the check always sees the live value."""
    response = JsonResponse({"build": settings.BUILD_ID})
    response["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------------- #
#  Guest side — the RSVP page (§2.5). Capability URL, no login, ever.
# --------------------------------------------------------------------------- #
def _guest_render(request, template, context=None, status=200):
    response = render(request, template, context or {}, status=status)
    # §8: capability tokens live in the URL — never leak them cross-origin via Referer.
    # "same-origin" (not "no-referrer") is deliberate: no-referrer makes browsers send
    # Origin: null on the RSVP/channel POSTs, which Django's HTTPS CSRF check rejects
    # (null ∉ CSRF_TRUSTED_ORIGINS, and no Referer to fall back on) → 403. "same-origin"
    # still sends nothing cross-origin (token never leaves our origin) but supplies a real
    # same-origin Origin so the POST passes CSRF.
    response["Referrer-Policy"] = "same-origin"
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
    coming = []
    going_count = maybe_count = 0
    if event.show_guest_list:
        # Everyone who's said going or maybe (going first). Uninvited (revoked)
        # guests never appear, whatever their last answer was.
        coming = [
            {"name": a.contact.greeting_name, "status": a.rsvp_status}
            for a in InvitationAttendee.objects.filter(
                invitation__event=event,
                rsvp_status__in=(
                    InvitationAttendee.Rsvp.GOING,
                    InvitationAttendee.Rsvp.MAYBE,
                ),
            )
            .exclude(invitation__state=Invitation.State.REVOKED)
            .select_related("contact")
            .order_by("rsvp_status", "contact__name")  # "going" sorts before "maybe"
        ]
        going_count = sum(c["status"] == InvitationAttendee.Rsvp.GOING for c in coming)
        maybe_count = len(coming) - going_count

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
        # Repopulate the channel-change form after a rejected submit (§2.5).
        "channel_kind": request.GET.get("kind", ""),
        "channel_value": request.GET.get("value", ""),
        "channel_member": request.GET.get("member", ""),
        "feedback_sent": request.GET.get("feedback") == "1",
        "feedback_error": request.GET.get("feedback") == "error",
        "channel_pending": invitation.channel_requests.filter(
            status=ContactChannel.Status.PROPOSED
        ).exists(),
        "plus_cap": event.plus_ones_cap or MAX_PLUS_ONES,
        "coming": coming,
        "going_count": going_count,
        "maybe_count": maybe_count,
        "google_url": google_calendar_url(event),
        "map_embed_url": google_maps_embed_url(event),
        "polls": _poll_display(event, invitation, voting_open=can_rsvp),
    }
    return _guest_render(request, "core/rsvp.html", context)


def _poll_display(event: Event, invitation: Invitation | None, *, voting_open: bool) -> list:
    """Per-poll display data (§2.7): options with counts + voter names (revoked
    envelopes excluded, like every other count) and this envelope's own ticks.
    Shared by the guest page and the dashboard (invitation=None there)."""
    polls = []
    poll_qs = event.polls.prefetch_related(
        Prefetch(
            "options",
            queryset=PollOption.objects.select_related("added_by__contact", "added_by__household"),
        ),
        Prefetch(
            "options__votes",
            queryset=PollVote.objects.exclude(invitation__state=Invitation.State.REVOKED)
            .select_related("invitation__contact", "invitation__household")
            .order_by("id"),
        ),
    )
    for poll in poll_qs:
        options = [
            {
                "option": option,
                "count": len(option.votes.all()),
                "names": [v.invitation.display_name for v in option.votes.all()],
                "mine": invitation is not None
                and any(v.invitation_id == invitation.pk for v in option.votes.all()),
            }
            for option in poll.options.all()
        ]
        polls.append(
            {"poll": poll, "options": options, "can_vote": voting_open and not poll.is_closed}
        )
    return polls


@require_POST
def rsvp_poll_vote(request, token, poll_pk):
    """Cast or refresh this envelope's ballot (§2.7). The submitted form is the whole
    truth: unticked options are removed, ticked ones added; single-choice polls keep
    one. A guest-typed option (when the poll allows it) is created and auto-ticked."""
    invitation = _get_invitation(token)
    if invitation.state == Invitation.State.REVOKED:
        return _guest_render(request, "core/rsvp_unavailable.html", status=410)
    event = invitation.event
    poll = get_object_or_404(Poll, pk=poll_pk, event=event)
    if poll.is_closed or event.status != Event.Status.ACTIVE or event.is_past:
        resp = HttpResponseForbidden("Voting is closed for this poll.")
        resp["Referrer-Policy"] = "no-referrer"
        return resp

    options = {o.pk: o for o in poll.options.all()}
    selected = []
    for raw in request.POST.getlist("option"):
        try:
            pk = int(raw)
        except (TypeError, ValueError):
            continue  # garbage ids are simply not votes
        if pk in options and options[pk] not in selected:
            selected.append(options[pk])

    new_text = request.POST.get("new_option", "").strip()[:MAX_POLL_OPTION_LEN]
    with transaction.atomic():
        if new_text and poll.allow_guest_options:
            existing = poll.options.filter(text__iexact=new_text).first()
            if existing:
                if existing not in selected:
                    selected.append(existing)
            elif len(options) < MAX_POLL_OPTIONS:  # at the cap the typed text is dropped
                selected.append(
                    PollOption.objects.create(poll=poll, text=new_text, added_by=invitation)
                )
        if not poll.multi_choice:
            selected = selected[-1:]  # radio pick + typed option together: the typed one wins
        PollVote.objects.filter(option__poll=poll, invitation=invitation).exclude(
            option__in=selected
        ).delete()
        for option in selected:
            PollVote.objects.get_or_create(option=option, invitation=invitation)
    return redirect(f"{invitation.rsvp_path}#poll-{poll.pk}")


@transaction.atomic
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
    # Only a form that actually carries the field can change the note — a POST
    # without it must not silently wipe what the guest wrote.
    note_submitted = "note" in request.POST
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

    if note_submitted and note != invitation.latest_note:
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
    member = request.POST.get("member", "")

    def error_redirect():
        # Carry the guest's attempt back so the reopened form keeps their chosen
        # channel + typed value instead of snapping back to the first option (§2.5).
        params = {"channel_error": "1", "kind": kind, "value": value}
        if member:
            params["member"] = member
        return redirect(f"{invitation.rsvp_path}?{urlencode(params)}")

    if kind not in ENTRY_KINDS:
        return error_redirect()

    # Whose channel: the single contact, or the chosen household member.
    if invitation.contact_id:
        contact = invitation.contact
    else:
        try:
            contact = invitation.household.members.filter(pk=member).first()
        except ValueError:  # non-numeric member id — same clean rejection as an unknown one
            contact = None
        if contact is None:
            return error_redirect()

    value, error = validate_channel_value(kind, value)
    if error:
        return error_redirect()

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


@require_POST
def rsvp_feedback(request, token):
    """Guest reports a problem or shares a thought from the RSVP page (§2.5). Saves a
    durable Feedback record (the source of truth, viewable in the admin) and fires a
    best-effort email to the organizer — the record stands even if the email fails. No
    login: the capability link authenticates, same as the RSVP itself."""
    invitation = _get_invitation(token)
    if invitation.state == Invitation.State.REVOKED:
        return _guest_render(request, "core/rsvp_unavailable.html", status=410)

    message = request.POST.get("message", "").strip()[:MAX_FEEDBACK_LEN]
    if not message:
        return redirect(f"{invitation.rsvp_path}?feedback=error")

    reply_email = request.POST.get("reply_email", "").strip()[:254]
    if reply_email:  # optional — keep only if it's a real address, else drop it silently
        _, err = validate_channel_value(ContactChannel.Kind.EMAIL, reply_email)
        if err:
            reply_email = ""

    feedback = Feedback.objects.create(
        invitation=invitation,
        event=invitation.event,
        message=message,
        reply_email=reply_email,
        page_path=request.META.get("HTTP_REFERER", "")[:255],
        user_agent=request.META.get("HTTP_USER_AGENT", "")[:400],
    )
    send_feedback_email(feedback)  # best-effort; record already saved
    return redirect(f"{invitation.rsvp_path}?feedback=1")


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
    # Surface delivery-level bounces (§9). The invite ladder can leave the envelope at
    # OPENED even after a bounce ("an open can't be overridden"), so a bounce would
    # otherwise be invisible. Take each invitation's *latest* delivery and warn when it
    # bounced — a later successful resend supersedes it and clears the warning.
    latest_delivery = {}
    for d in (
        Delivery.objects.filter(invitation__event=event)
        .select_related("invitation__contact", "invitation__household")
        .order_by("id")
    ):
        latest_delivery[d.invitation_id] = d
    bounced = [d for d in latest_delivery.values() if d.status == Delivery.Status.BOUNCED]
    bounced_inv_ids = {d.invitation_id for d in bounced}

    for inv in invitations:
        inv.rsvp_url = request.build_absolute_uri(inv.rsvp_path)
        inv.route_email = bool(email_channels(inv))
        inv.route_assisted = bool(assisted_channels(inv))
        inv.has_bounce = inv.id in bounced_inv_ids

    # The standing to-do (§8.4): what's sitting in the outbox. Nothing sends itself any
    # more, so this prompt is the thing that stops queued invites being forgotten — it's
    # load-bearing, not decoration. One query, split by what each half needs from you.
    outstanding = Delivery.objects.filter(invitation__event=event, status__in=Delivery.OUTSTANDING)
    waiting = {
        "email": outstanding.filter(
            status=Delivery.Status.PENDING, channel_kind=ContactChannel.Kind.EMAIL
        ).count(),
        "manual": outstanding.filter(
            status=Delivery.Status.PENDING, channel_kind__in=ASSISTED_KINDS
        ).count(),
        "blocked": outstanding.filter(status=Delivery.Status.BLOCKED).count(),
    }
    waiting["total"] = waiting["email"] + waiting["manual"] + waiting["blocked"]

    # Revoked envelopes stay visible in the table (greyed) but are out of every
    # count — uninvited guests aren't catered for (§2.2).
    attendee_qs = InvitationAttendee.objects.filter(invitation__event=event).exclude(
        invitation__state=Invitation.State.REVOKED
    )
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
        "bounced": bounced,
        "going": by_status.get(InvitationAttendee.Rsvp.GOING, 0),
        "maybe": by_status.get(InvitationAttendee.Rsvp.MAYBE, 0),
        "cant": by_status.get(InvitationAttendee.Rsvp.CANT, 0),
        "no_reply": by_status.get(InvitationAttendee.Rsvp.NO_REPLY, 0),
        "invited": attendee_qs.count(),
        "opened": event.invitations.exclude(state=Invitation.State.REVOKED)
        .filter(opened_at__isnull=False)
        .count(),
        "expected": event.expected_headcount,
        "pending_channels": ContactChannel.objects.filter(status=ContactChannel.Status.PROPOSED)
        .select_related("contact", "requested_via__event")
        .order_by("created_at"),
        "notes": sorted(
            (i for i in invitations if i.latest_note),
            key=lambda i: i.latest_note_at or i.updated_at,
            reverse=True,
        ),
        "history": RsvpEvent.objects.filter(attendee__invitation__event=event).select_related(
            "attendee__contact", "actor_user"
        )[:30],
        "reminder_due": reminder_due,
        "waiting": waiting,
        "polls": _poll_display(event, None, voting_open=False),
        "result": {k: request.GET.get(k) for k in ("did", "sent", "failed", "skipped", "msg")}
        if request.GET.get("did")
        else None,
    }
    return render(request, "core/dashboard.html", context)


def _activate_if_draft(event) -> None:
    """A draft flips active the moment guests are put in the queue for it (§2.1).

    This used to hang off the first successful *send*. It can't any more — queueing and
    sending are separate now, and `can_rsvp` requires ACTIVE, so an event left in draft
    while its invites went out would hand every guest a link they can't answer.
    """
    if event.status == Event.Status.DRAFT:
        event.status = Event.Status.ACTIVE
        event.save(update_fields=["status", "updated_at"])


# Which audience each action queues to, and what message they get.
SEND_ACTIONS = {
    "invites": ("invites", "invite"),
    "retry": ("retryable", "invite"),
    "nudge": ("non_responders", "nudge"),
    "update": ("notified", "update"),
    "cancel": ("notified", "cancellation"),
    "reminder": ("reminder", "reminder"),
}


@staff_member_required
@require_http_methods(["GET", "POST"])
def event_send(request, pk):
    """Queue-messages screen (§2.3/§2.4). Every action here **queues and stops** — the
    messages then sit in the outbox until the organizer sends them from the Messages
    screen. Nothing goes out as a side effect of pressing a button on this page."""
    event = get_object_or_404(Event, pk=pk)
    targets = send_targets(event)

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action not in SEND_ACTIONS:
            return HttpResponseForbidden("Unknown action")
        audience, message_kind = SEND_ACTIONS[action]
        base_url = request.build_absolute_uri("/").rstrip("/")

        if action == "invites":
            _activate_if_draft(event)
        elif action == "cancel":
            if event.status != Event.Status.CANCELLED:
                event.status = Event.Status.CANCELLED
                event.save(update_fields=["status", "updated_at"])
            # Clear the queue *before* queueing the notice: an invite going out after a
            # cancellation is the worst thing this app could do (§9).
            cancel_pending(event)
        elif action == "retry":
            # A retry is a re-queue, not a re-send: the row goes back in the outbox and
            # leaves with the next batch, so a bad address can be fixed in between.
            Delivery.objects.filter(invitation__event=event, status__in=Delivery.RETRYABLE).update(
                status=Delivery.Status.CANCELLED, updated_at=timezone.now()
            )

        result = enqueue(targets[audience], message_kind, base_url)
        msgs = reverse("event-messages", args=[event.pk])
        return redirect(f"{msgs}?{urlencode({'did': action, **result})}")

    return render(request, "core/send.html", {"event": event, "targets": targets})


@staff_member_required
@require_http_methods(["GET", "POST"])
def event_invite(request, pk):
    """Add guests to this event (§2.2): the event-side counterpart to the Contacts
    admin action. Lists contacts not yet on this event, plus households you can invite
    as a single shared-link envelope; ticking either creates the invitation (attendees
    + token via Invitation.save). Email invitees are sent their invite **immediately**
    (§2.3) — adding *is* inviting; assisted (WhatsApp/Messenger) guests still need the
    send queue, which the dashboard prompt points to."""
    event = get_object_or_404(Event, pk=pk)

    if request.method == "POST":
        new_invites = []

        # Whole-household envelopes: one shared link covering every member (§2.5).
        # Members of a chosen household are covered by that link, so an individual
        # tick for them is ignored — nobody gets two invitations.
        selected_hh = list(
            Household.objects.filter(pk__in=request.POST.getlist("households")).prefetch_related(
                "members"
            )
        )
        already_hh = set(
            Invitation.objects.filter(event=event, household__in=selected_hh).values_list(
                "household_id", flat=True
            )
        )
        covered_member_ids = {m.pk for hh in selected_hh for m in hh.members.all()}
        for household in selected_hh:
            if household.pk not in already_hh:
                new_invites.append(Invitation.objects.create(event=event, household=household))

        selected = Contact.objects.filter(pk__in=request.POST.getlist("contacts")).exclude(
            pk__in=covered_member_ids
        )
        already = set(
            Invitation.objects.filter(event=event, contact__in=selected).values_list(
                "contact_id", flat=True
            )
        )
        for contact in selected:
            if contact.pk not in already:  # skip anyone already invited (defensive)
                new_invites.append(Invitation.objects.create(event=event, contact=contact))

        # Queue their invites (§2.3). Adding no longer *sends* — the messages wait in
        # the outbox so you can see them before they go — so we land on Messages rather
        # than the dashboard, where the send button is one tap away.
        result = {"queued": 0, "blocked": 0, "already_queued": 0}
        if new_invites and event.status != Event.Status.CANCELLED:
            _activate_if_draft(event)
            result = enqueue(new_invites, "invite", request.build_absolute_uri("/").rstrip("/"))

        created = len(new_invites)
        if not created:
            msg = "no new guests"
        else:
            parts = [f"{created} guest{'' if created == 1 else 's'} added"]
            if result["queued"]:
                parts.append(
                    f"{result['queued']} message{'' if result['queued'] == 1 else 's'} queued"
                )
            if result["blocked"]:
                parts.append(f"{result['blocked']} with no channel")
            msg = " · ".join(parts)
        target = "event-messages" if result["queued"] or result["blocked"] else "event-dashboard"
        return redirect(
            f"{reverse(target, args=[event.pk])}?{urlencode({'did': 'invited', 'msg': msg})}"
        )

    q = request.GET.get("q", "").strip()
    contacts = (
        Contact.objects.exclude(invitations__event=event)  # not already on this event
        .select_related("household")
        .prefetch_related("channels")
        .order_by("household__name", "name")
    )
    if q:
        contacts = contacts.filter(Q(name__icontains=q) | Q(nickname__icontains=q))

    # Households already invited as an envelope: their members are covered, so drop them.
    invited_household_ids = set(
        Invitation.objects.filter(event=event, household__isnull=False).values_list(
            "household_id", flat=True
        )
    )
    # Group the uninvited contacts under their household (offer the whole-household
    # option there) and collect the rest as loose individuals.
    grouped: dict = {}
    loose = []
    for contact in contacts:
        household = contact.household
        if household and household.pk not in invited_household_ids:
            grouped.setdefault(household, []).append(contact)
        elif household is None:
            loose.append(contact)
        # else: household already invited as an envelope → member is covered, hide it
    household_groups = sorted(grouped.items(), key=lambda kv: kv[0].name.lower())

    return render(
        request,
        "core/invite.html",
        {"event": event, "household_groups": household_groups, "loose": loose, "q": q},
    )


# --------------------------------------------------------------------------- #
#  Messages — the outbox (§2.3). One screen per event listing every message: what
#  is waiting to go, what needs doing by hand, who can't be reached, what failed,
#  and everything already sent. Email leaves in a batch on one button; assisted
#  messages are marked sent by the organizer, never by the app.
# --------------------------------------------------------------------------- #
MESSAGE_KINDS = [k for k, _ in Delivery.MessageKind.choices]


def _outbox(event):
    return Delivery.objects.filter(invitation__event=event).select_related(
        "invitation__contact", "invitation__household", "invitation__event", "channel", "sent_by"
    )


def _decorate(deliveries, base_url):
    """Attach the per-row display bits the template can't work out on its own."""
    for delivery in deliveries:
        invitation = delivery.invitation
        delivery.rsvp_url = rsvp_url(invitation, base_url)
        delivery.is_assisted = delivery.channel_kind in ASSISTED_KINDS
        delivery.recipient = invitation.display_name
        # A household envelope carrying several assisted copies is two taps of the same
        # link — worth saying on the card so it doesn't look like a duplicate.
        delivery.share = (
            share_payload_from(delivery.body, delivery.rsvp_url) if delivery.is_assisted else None
        )
        delivery.wa_url = (
            wa_link(delivery.address_used, delivery.share["text"])
            if delivery.channel_kind == ContactChannel.Kind.WHATSAPP and delivery.share
            else None
        )
        # "Written before the event changed": only meaningful for edited rows, since
        # unedited ones are re-rendered at send time anyway (§9).
        delivery.is_stale = delivery.is_edited and invitation.event.updated_at > delivery.updated_at
    return deliveries


@staff_member_required
def event_messages(request, pk):
    """The outbox screen. Read-only: every mutation is a POST to `message-action`."""
    event = get_object_or_404(Event, pk=pk)
    base_url = request.build_absolute_uri("/").rstrip("/")
    rows = _decorate(list(_outbox(event).order_by("id")), base_url)

    by_status = {}
    for row in rows:
        by_status.setdefault(row.status, []).append(row)
    pending = by_status.get(Delivery.Status.PENDING, [])

    # Manual rows sort deferred-last, so "skip for now" actually sinks something.
    manual = sorted(
        (r for r in pending if r.is_assisted),
        key=lambda r: (r.deferred_at is not None, r.deferred_at or r.created_at, r.id),
    )

    kind_filter = request.GET.get("kind", "")
    status_filter = request.GET.get("status", "")
    history = [r for r in rows if r.status not in Delivery.OUTSTANDING]
    if kind_filter in MESSAGE_KINDS:
        history = [r for r in history if r.message_kind == kind_filter]
    if status_filter in Delivery.Status.values:
        history = [r for r in history if r.status == status_filter]

    context = {
        "event": event,
        "pending_email": [r for r in pending if not r.is_assisted],
        "manual": manual,
        "deferred_count": sum(1 for r in manual if r.deferred_at),
        "blocked": by_status.get(Delivery.Status.BLOCKED, []),
        "attention": by_status.get(Delivery.Status.FAILED, [])
        + by_status.get(Delivery.Status.BOUNCED, []),
        "history": list(reversed(history)),
        "sent_count": len(by_status.get(Delivery.Status.SENT, [])),
        "kind_filter": kind_filter,
        "status_filter": status_filter,
        "kind_choices": Delivery.MessageKind.choices,
        "status_choices": Delivery.Status.choices,
        "result": {
            k: request.GET.get(k)
            for k in ("did", "sent", "failed", "queued", "blocked", "already_queued", "msg")
        }
        if request.GET.get("did")
        else None,
    }
    return render(request, "core/messages.html", context)


@staff_member_required
@require_POST
def event_send_pending(request, pk):
    """Send every pending email for this event (§7.2). The one automated send."""
    event = get_object_or_404(Event, pk=pk)
    result = send_pending_emails(
        event, request.build_absolute_uri("/").rstrip("/"), actor=request.user
    )
    msgs = reverse("event-messages", args=[event.pk])
    return redirect(f"{msgs}?{urlencode({'did': 'sent', **result})}")


@staff_member_required
@require_POST
def message_action(request, pk):
    """Per-row actions: mark sent, defer, cancel, retry, revert (§7.4)."""
    delivery = get_object_or_404(_outbox_row_qs(), pk=pk)
    event = delivery.invitation.event
    action = request.POST.get("action", "")
    back = request.POST.get("next") or reverse("event-messages", args=[event.pk])

    if action == "mark_sent":
        mark_sent(delivery, request.user)
        msg = f"Marked sent to {delivery.invitation.display_name}"
    elif action == "defer":
        delivery.deferred_at = timezone.now()
        delivery.save(update_fields=["deferred_at", "updated_at"])
        msg = "Skipped for now"
    elif action == "undefer":
        delivery.deferred_at = None
        delivery.save(update_fields=["deferred_at", "updated_at"])
        msg = "Back in the queue"
    elif action == "cancel":
        delivery.status = Delivery.Status.CANCELLED
        delivery.save(update_fields=["status", "updated_at"])
        msg = "Won't be sent"
    elif action == "revert":
        # Back to the template, and back to auto-refreshing from the event.
        base_url = request.build_absolute_uri("/").rstrip("/")
        subject, text = render_message(
            delivery.message_kind, delivery.invitation, rsvp_url(delivery.invitation, base_url)
        )
        delivery.subject, delivery.body, delivery.is_edited = subject, text, False
        delivery.save(update_fields=["subject", "body", "is_edited", "updated_at"])
        msg = "Reverted to the template"
    elif action == "retry":
        if delivery.status not in Delivery.RETRYABLE:
            return HttpResponseForbidden("Only a failed or bounced message can be retried")
        # Re-queue rather than re-send: it goes out with the next batch, so a bad address
        # can be fixed in between. A channel deleted since is a blocked row, not a crash.
        delivery.status = (
            Delivery.Status.PENDING if delivery.channel_id else Delivery.Status.BLOCKED
        )
        delivery.error = ""
        delivery.sent_at = None
        delivery.provider_message_id = ""
        delivery.save(
            update_fields=["status", "error", "sent_at", "provider_message_id", "updated_at"]
        )
        msg = "Back in the queue"
    else:
        return HttpResponseForbidden("Unknown action")

    return redirect(f"{back}{'&' if '?' in back else '?'}{urlencode({'did': action, 'msg': msg})}")


def _outbox_row_qs():
    return Delivery.objects.select_related(
        "invitation__contact", "invitation__household", "invitation__event", "channel"
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def message_edit(request, pk):
    """Rewrite one queued message in your own words (§7.4).

    The one thing an edit may not do is drop the RSVP link. Every message this app sends
    exists to deliver that link (§4) — a message without it is a message that can't be
    answered — so a body missing it is rejected rather than quietly repaired.
    """
    delivery = get_object_or_404(_outbox_row_qs(), pk=pk)
    if delivery.status not in Delivery.OUTSTANDING:
        return HttpResponseForbidden("Only a message that hasn't gone out can be edited")
    url = rsvp_url(delivery.invitation, request.build_absolute_uri("/").rstrip("/"))
    error = None

    if request.method == "POST":
        subject = request.POST.get("subject", "").strip()
        body = request.POST.get("body", "").strip()
        if not body:
            error = "The message can't be empty."
        elif url not in body:
            error = (
                "Keep their personal link in the message — it's the only way they can "
                f"reply. Paste it back in: {url}"
            )
        else:
            delivery.subject, delivery.body, delivery.is_edited = subject, body, True
            delivery.save(update_fields=["subject", "body", "is_edited", "updated_at"])
            back = reverse("event-messages", args=[delivery.invitation.event_id])
            return redirect(f"{back}?{urlencode({'did': 'edit', 'msg': 'Message updated'})}")
        delivery.subject, delivery.body = subject, body  # redisplay what they typed

    return render(
        request,
        "core/message_edit.html",
        {
            "event": delivery.invitation.event,
            "m": delivery,
            "rsvp_url": url,
            "error": error,
            "is_email": delivery.channel_kind == ContactChannel.Kind.EMAIL,
        },
    )


# --------------------------------------------------------------------------- #
#  The walkthrough (§8.2) — the same pending manual rows, one card at a time, for
#  working through a batch on a phone without hunting the list for your place.
# --------------------------------------------------------------------------- #
def _walk_queue(event, base_url):
    """Pending manual rows in card order: un-deferred first, oldest first."""
    rows = [
        r
        for r in _outbox(event).filter(status=Delivery.Status.PENDING).order_by("id")
        if r.channel_kind in ASSISTED_KINDS
    ]
    rows.sort(key=lambda r: (r.deferred_at is not None, r.deferred_at or r.created_at, r.id))
    return _decorate(rows, base_url)


@staff_member_required
def event_queue(request, pk):
    """One assisted message at a time. Marking sent or deferring is a POST to
    `message-action`, which lands back here — the card is a view of the outbox, not a
    parallel piece of state, so the list and the walk can never disagree."""
    event = get_object_or_404(Event, pk=pk)
    base_url = request.build_absolute_uri("/").rstrip("/")
    queue = _walk_queue(event, base_url)
    try:
        n = max(0, int(request.GET.get("n", 0)))
    except ValueError:
        n = 0

    item = queue[n] if n < len(queue) else None
    if item:
        item.household_assisted = sum(1 for r in queue if r.invitation_id == item.invitation_id)
    return render(
        request,
        "core/queue.html",
        {
            "event": event,
            "item": item,
            "n": n,
            "total": len(queue),
            "deferred_count": sum(1 for r in queue if r.deferred_at),
            "walk_url": reverse("event-queue", args=[event.pk]),
        },
    )


# --------------------------------------------------------------------------- #
#  Per-guest row actions + organizer override (§2.3) and the channel-change
#  approval queue (§2.5) — all POST-only, all staff-only, all land back on the
#  dashboard with a banner.
# --------------------------------------------------------------------------- #
def _dash(event_pk, did, **params):
    # Values include free text (contact names, messages) — always URL-encode.
    query = urlencode({"did": did, **params})
    return redirect(f"{reverse('event-dashboard', args=[event_pk])}?{query}")


@staff_member_required
@require_POST
def invitation_action(request, pk):
    invitation = get_object_or_404(Invitation.objects.select_related("event"), pk=pk)
    action = request.POST.get("action", "")
    base_url = request.build_absolute_uri("/").rstrip("/")

    if action == "revoke":
        invitation.advance_state(Invitation.State.REVOKED)
        # Uninviting someone must not leave their invite sitting in the queue, waiting to
        # be sent to a guest who is no longer invited (§9).
        dropped = cancel_pending_for(invitation)
        msg = "Invitation revoked — link now dead"
        if dropped:
            msg += f" · {dropped} queued message{'' if dropped == 1 else 's'} dropped"
        return _dash(invitation.event_id, action, msg=msg)
    if action == "regenerate":
        # New capability token: the old link is dead, the invitation lives on (§2.3).
        invitation.token = make_token()
        invitation.save(update_fields=["token", "updated_at"])
        # Queued messages still carry the *old* link in their snapshot. Re-render the
        # unedited ones now; an edited one is flagged so the organizer fixes it by hand
        # rather than having their words silently rewritten.
        _refresh_queued_links(invitation, base_url)
        return _dash(invitation.event_id, action, msg="New link generated — old one is dead")
    if action in ("resend", "nudge"):
        kind = "invite" if action == "resend" else "nudge"
        result = enqueue([invitation], kind, base_url)
        return _dash(invitation.event_id, action, **result)
    return HttpResponseForbidden("Unknown action")


def _refresh_queued_links(invitation, base_url: str) -> None:
    """Re-snapshot pending messages after the token changed, so nothing goes out
    pointing at a link that's already dead."""
    for delivery in invitation.deliveries.filter(status=Delivery.Status.PENDING):
        if delivery.is_edited:
            continue
        subject, text = render_message(
            delivery.message_kind, invitation, rsvp_url(invitation, base_url)
        )
        delivery.subject, delivery.body = subject, text
        delivery.save(update_fields=["subject", "body", "updated_at"])


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


def _requeue_blocked_for(contact, request) -> int:
    """Re-queue this contact's blocked messages now that they have a usable channel.

    Covers both shapes of envelope: their own, and any household one they're part of —
    a household is blocked only when *nobody* in it was reachable, so one member getting
    a channel unblocks the whole envelope.
    """
    blocked = Delivery.objects.filter(
        status=Delivery.Status.BLOCKED,
        invitation__state__in=[s for s in Invitation.State.values if s != Invitation.State.REVOKED],
    ).filter(Q(invitation__contact=contact) | Q(invitation__household=contact.household_id))
    base_url = request.build_absolute_uri("/").rstrip("/")
    count = 0
    for row in blocked.select_related("invitation"):
        result = enqueue([row.invitation], row.message_kind, base_url)
        if result["queued"]:
            # The blocked row did its job — it's superseded by real ones now.
            row.status = Delivery.Status.CANCELLED
            row.save(update_fields=["status", "updated_at"])
            count += result["queued"]
    return count


@staff_member_required
@require_POST
def channel_request_action(request, pk):
    """One-tap approve / reject for a guest-requested channel (§2.5). Approval makes
    it the contact's preferred channel — future sends follow it."""
    channel = get_object_or_404(ContactChannel, pk=pk, status=ContactChannel.Status.PROPOSED)
    action = request.POST.get("action", "")

    # Where to send the organizer back to: the home page lists every request in one
    # place (§2.5) and posts `home=1`; an event dashboard posts its `event` pk.
    home = bool(request.POST.get("home"))
    event_pk = None
    if not home:
        # The event pk only routes the redirect — but reverse() 500s on garbage, so
        # resolve it properly (falls back to the requesting invitation's event).
        try:
            event = Event.objects.filter(pk=int(request.POST.get("event", ""))).first()
        except (TypeError, ValueError):
            event = None
        if event is None and channel.requested_via_id:
            event = channel.requested_via.event
        if event is None:
            return HttpResponseForbidden("Unknown event")
        event_pk = event.pk

    def done(did, msg):
        if home:
            return redirect(f"{reverse('admin-home')}?{urlencode({'did': did, 'msg': msg})}")
        return _dash(event_pk, did, msg=msg)

    if action == "approve":
        with transaction.atomic():
            channel.contact.channels.filter(is_preferred=True).exclude(pk=channel.pk).update(
                is_preferred=False
            )
            channel.status = ContactChannel.Status.ACTIVE
            channel.is_preferred = True
            channel.save(update_fields=["status", "is_preferred", "updated_at"])
        msg = f"{channel.contact.name} → {channel.get_kind_display()}"
        # Approving a channel for someone we couldn't reach is exactly the moment their
        # stuck message becomes sendable — re-queue it rather than leaving them parked
        # under "can't send" with the fix already in place (§9).
        requeued = _requeue_blocked_for(channel.contact, request)
        if requeued:
            msg += f" · {requeued} queued message{'' if requeued == 1 else 's'} unblocked"
        return done(action, msg)
    if action == "reject":
        name = channel.contact.name
        channel.delete()
        return done(action, f"Request from {name} rejected")
    return HttpResponseForbidden("Unknown action")


# --------------------------------------------------------------------------- #
#  Polls (§2.7) — created and managed from the dashboard; guests vote on the
#  RSVP page via their capability link, one ballot per envelope.
# --------------------------------------------------------------------------- #
@staff_member_required
@require_POST
def event_poll_create(request, pk):
    """Create a poll from the dashboard (§2.7): question + one option per line.
    Options are deduped case-insensitively and capped; blank lines drop out."""
    event = get_object_or_404(Event, pk=pk)
    question = request.POST.get("question", "").strip()[:MAX_POLL_QUESTION_LEN]
    texts: list[str] = []
    for line in request.POST.get("options", "").splitlines():
        text = line.strip()[:MAX_POLL_OPTION_LEN]
        if text and text.lower() not in (t.lower() for t in texts):
            texts.append(text)
    texts = texts[:MAX_POLL_OPTIONS]
    if not question or not texts:
        return _dash(event.pk, "poll_error", msg="A poll needs a question and at least one option")
    with transaction.atomic():
        poll = Poll.objects.create(
            event=event,
            question=question,
            multi_choice=request.POST.get("multi_choice") == "1",
            allow_guest_options=request.POST.get("allow_guest_options") == "1",
            created_by=request.user,
        )
        for text in texts:
            PollOption.objects.create(poll=poll, text=text)
    return _dash(event.pk, "poll_created", msg=f"Poll created: {question}")


@staff_member_required
@require_POST
def poll_action(request, pk):
    """Dashboard poll controls (§2.7): close/reopen, delete, remove one option."""
    poll = get_object_or_404(Poll.objects.select_related("event"), pk=pk)
    event_pk = poll.event_id
    action = request.POST.get("action", "")
    if action in ("close", "reopen"):
        poll.is_closed = action == "close"
        poll.save(update_fields=["is_closed", "updated_at"])
        return _dash(event_pk, action, msg=f"Poll {'closed' if poll.is_closed else 'reopened'}")
    if action == "delete":
        poll.delete()
        return _dash(event_pk, action, msg="Poll deleted")
    if action == "remove_option":
        option = get_object_or_404(PollOption, pk=_posted_pk(request, "option"), poll=poll)
        option.delete()  # votes CASCADE — a removed option takes its ticks with it
        return _dash(event_pk, action, msg=f"Option removed: {option.text}")
    return HttpResponseForbidden("Unknown action")


# --------------------------------------------------------------------------- #
#  Organizer home (§2.6) — the friendly landing behind Access: jump to contacts,
#  your events (each linking to its dashboard), and out to the full Django admin.
#  The Django admin index links here and `admin.site.site_url` points at it, so an
#  organizer reaches it from the habitual /admin without remembering a URL.
# --------------------------------------------------------------------------- #
@staff_member_required
def admin_home(request):
    now = timezone.now()
    events = list(Event.objects.order_by("starts_at"))
    upcoming = [e for e in events if e.starts_at >= now and e.status != Event.Status.CANCELLED]
    # Everything else (already happened, or cancelled) — most-recent first, capped.
    past = [e for e in reversed(events) if e.starts_at < now or e.status == Event.Status.CANCELLED]
    context = {
        "upcoming": upcoming,
        "past": past[:8],
        "past_total": len(past),
        "contacts_count": Contact.objects.count(),
        # Every guest-requested channel change awaiting approval, in one place (§2.5) —
        # approving here sets it as the contact's preferred channel.
        "pending_channels": ContactChannel.objects.filter(status=ContactChannel.Status.PROPOSED)
        .select_related("contact", "requested_via__event")
        .order_by("created_at"),
        "result": {k: request.GET.get(k) for k in ("did", "msg")}
        if request.GET.get("did")
        else None,
    }
    return render(request, "core/admin_home.html", context)


# --------------------------------------------------------------------------- #
#  Contacts & households (§2.2) — hand-built organizer flow that replaces the
#  Django admin for the everyday "add a person / build a family" job. Admin stays
#  registered as CRUD backup. Lives under /admin/ so the one Access rule gates it.
# --------------------------------------------------------------------------- #
CHANNEL_KIND_CHOICES = [(k.value, k.label) for k in ContactChannel.Kind if k in ENTRY_KINDS]
MAX_NAME_LEN = 120
MAX_NICK_LEN = 60
MAX_LABEL_LEN = 60
# A birth year is a light "age N" hint for kids (§2.5), not identity — bound it sanely.
MIN_BIRTH_YEAR = 1900


def _contacts_redirect(msg: str):
    return redirect(f"{reverse('contacts-home')}?{urlencode({'msg': msg})}")


def _parse_year(raw: str):
    """A 4-digit birth year in a sane range, else None (forgiving — never an error)."""
    try:
        year = int(raw)
    except (TypeError, ValueError):
        return None
    return year if MIN_BIRTH_YEAR <= year <= timezone.now().year else None


def _blank_channel_row():
    return {
        "id": "",
        "kind": ContactChannel.Kind.EMAIL,
        "value": "",
        "label": "",
        "preferred": False,
    }


def _channel_row_from(channel: ContactChannel) -> dict:
    return {
        "id": channel.pk,
        "kind": channel.kind,
        "value": channel.value,
        "label": channel.label,
        "preferred": channel.is_preferred,
    }


def _posted_channel_rows(request) -> list[dict]:
    """Parse the contact form's parallel-array channel inputs into aligned dicts. Every
    row renders all fields (selects + hidden inputs always submit), so the lists stay
    index-aligned; `preferred` names the chosen row's index. Delete-flagged rows drop."""
    ids = request.POST.getlist("channel_id")
    kinds = request.POST.getlist("channel_kind")
    values = request.POST.getlist("channel_value")
    labels = request.POST.getlist("channel_label")
    deletes = request.POST.getlist("channel_delete")
    try:
        preferred_idx = int(request.POST.get("preferred", "-1"))
    except (TypeError, ValueError):
        preferred_idx = -1
    rows = []
    for i, kind in enumerate(kinds):
        if (deletes[i] if i < len(deletes) else "0") == "1":
            continue
        rows.append(
            {
                "id": (ids[i] if i < len(ids) else "").strip(),
                "kind": kind,
                "value": (values[i] if i < len(values) else "").strip()[:254],
                "label": (labels[i] if i < len(labels) else "").strip()[:MAX_LABEL_LEN],
                "preferred": i == preferred_idx,
            }
        )
    return rows


def _kept_channel_rows(rows: list[dict]) -> list[dict]:
    """Rows that describe a real channel: Messenger needs no value; the rest without one
    are treated as empty leftovers and silently skipped (forgiving spare-row behaviour)."""
    return [r for r in rows if r["kind"] == ContactChannel.Kind.MESSENGER or r["value"]]


def _contact_form_context(contact, *, rows, fields, error=None):
    return {
        "contact": contact,
        "households": Household.objects.order_by("name"),
        "kind_choices": CHANNEL_KIND_CHOICES,
        "messenger_kind": ContactChannel.Kind.MESSENGER,
        "rows": rows,
        "fields": fields,
        "error": error,
        "proposed": (
            list(contact.channels.filter(status=ContactChannel.Status.PROPOSED)) if contact else []
        ),
    }


def _contact_fields_from(contact) -> dict:
    return {
        "name": contact.name,
        "nickname": contact.nickname,
        "birth_year": contact.birth_year or "",
        "household": contact.household_id or "",
        "notes": contact.notes,
        "tags": ", ".join(contact.tags.values_list("name", flat=True)),
    }


def _contact_fields_posted(request) -> dict:
    return {
        "name": request.POST.get("name", "").strip()[:MAX_NAME_LEN],
        "nickname": request.POST.get("nickname", "").strip()[:MAX_NICK_LEN],
        "birth_year": request.POST.get("birth_year", "").strip(),
        "household": request.POST.get("household", "").strip(),
        "notes": request.POST.get("notes", "").strip(),
        "tags": request.POST.get("tags", "").strip(),
    }


def _set_tags(contact, raw: str) -> None:
    names = [t.strip()[:50] for t in raw.split(",") if t.strip()]
    tags = [Tag.objects.get_or_create(name=n)[0] for n in names]
    contact.tags.set(tags)


def _block_pending_on_deleted_channels(dropped) -> None:
    """Move queued messages off channels about to be deleted.

    Only one blocked row may exist per (invitation, message kind) — deleting a channel
    SET_NULLs the delivery, and a household that loses both parents' numbers at once
    would otherwise trip that constraint mid-delete. The first row becomes the "can't
    send" entry; the rest collapse into it as cancelled, since they'd all say the same
    thing.
    """
    rows = list(Delivery.objects.filter(channel__in=dropped, status=Delivery.Status.PENDING))
    taken = set(
        Delivery.objects.filter(
            invitation_id__in=[r.invitation_id for r in rows],
            status=Delivery.Status.BLOCKED,
            channel__isnull=True,
        ).values_list("invitation_id", "message_kind")
    )
    for row in rows:
        key = (row.invitation_id, row.message_kind)
        if key in taken:
            row.status = Delivery.Status.CANCELLED
        else:
            row.status = Delivery.Status.BLOCKED
            row.address_used = ""
            row.channel_kind = ""
            taken.add(key)
        row.save(update_fields=["status", "address_used", "channel_kind", "updated_at"])


def _save_channels(contact, rows: list[dict]) -> None:
    """Diff the submitted rows against the contact's ACTIVE channels: update by id,
    create new ones, delete ACTIVE channels no longer present (preserves Delivery audit
    rows — Delivery.channel is SET_NULL). Guest PROPOSED channels are never touched.
    Enforces one preferred by unsetting all first, then flagging the chosen row."""
    existing = {c.pk: c for c in contact.channels.filter(status=ContactChannel.Status.ACTIVE)}
    seen_ids: set[int] = set()
    preferred_channel = None
    for row in rows:
        value, _ = validate_channel_value(row["kind"], row["value"])  # already validated
        try:
            pk = int(row["id"])
        except (TypeError, ValueError):
            pk = None
        channel = existing.get(pk) if pk else None
        if channel is None:
            channel = ContactChannel(contact=contact, source=ContactChannel.Source.ORGANIZER)
        channel.kind = row["kind"]
        channel.value = value
        channel.label = row["label"]
        channel.is_preferred = False
        channel.status = ContactChannel.Status.ACTIVE
        channel.save()
        seen_ids.add(channel.pk)
        if row["preferred"]:
            preferred_channel = channel
    dropped = [channel for pk, channel in existing.items() if pk not in seen_ids]
    # A queued message on a channel you just deleted must not still go out: address_used
    # is a snapshot, so the row would happily send to the address you removed. Park it as
    # blocked and let the organizer re-queue it on whatever replaced the channel (§9).
    if dropped:
        _block_pending_on_deleted_channels(dropped)
    for channel in dropped:
        channel.delete()
    if preferred_channel is not None:
        preferred_channel.is_preferred = True
        preferred_channel.save(update_fields=["is_preferred", "updated_at"])


@staff_member_required
def contacts_home(request):
    """Searchable contact list grouped by household (§2.2) — the front door to the
    hand-built add-contact / add-household flows."""
    q = request.GET.get("q", "").strip()
    households = list(
        Household.objects.prefetch_related(
            Prefetch(
                "members",
                queryset=Contact.objects.prefetch_related("channels").order_by("name"),
            )
        ).order_by("name")
    )
    loose = list(
        Contact.objects.filter(household__isnull=True).prefetch_related("channels").order_by("name")
    )
    total = Contact.objects.count()
    if q:
        ql = q.lower()
        match = lambda c: ql in c.name.lower() or ql in c.nickname.lower()  # noqa: E731
        groups = []
        for hh in households:
            members = list(hh.members.all())
            if ql in hh.name.lower():
                groups.append((hh, members))
            elif hit := [m for m in members if match(m)]:
                groups.append((hh, hit))
        loose = [c for c in loose if match(c)]
    else:
        groups = [(hh, list(hh.members.all())) for hh in households]
    context = {
        "groups": groups,
        "loose": loose,
        "q": q,
        "total": total,
        "msg": request.GET.get("msg"),
    }
    return render(request, "core/contacts_home.html", context)


@staff_member_required
@require_http_methods(["GET", "POST"])
def contact_new(request):
    return _contact_form(request, None)


@staff_member_required
@require_http_methods(["GET", "POST"])
def contact_edit(request, pk):
    contact = get_object_or_404(Contact.objects.prefetch_related("channels", "tags"), pk=pk)
    return _contact_form(request, contact)


def _contact_form(request, contact):
    """Shared GET/POST handler for adding and editing a contact + its channels (§2.2)."""
    if request.method == "POST":
        fields = _contact_fields_posted(request)
        rows = _kept_channel_rows(_posted_channel_rows(request))
        error = None
        if not fields["name"]:
            error = "A contact needs a name."
        else:
            for row in rows:  # validate every kept channel before writing anything
                _, err = validate_channel_value(row["kind"], row["value"])
                if err:
                    error = err
                    break
        if error:
            return render(
                request,
                "core/contact_form.html",
                _contact_form_context(
                    contact, rows=rows or [_blank_channel_row()], fields=fields, error=error
                ),
            )
        household = None
        if fields["household"]:
            household = Household.objects.filter(pk=fields["household"]).first()
        with transaction.atomic():
            if contact is None:
                contact = Contact(created_by=request.user)
            contact.name = fields["name"]
            contact.nickname = fields["nickname"]
            contact.birth_year = _parse_year(fields["birth_year"])
            contact.household = household
            contact.notes = fields["notes"]
            contact.save()
            _set_tags(contact, fields["tags"])
            _save_channels(contact, rows)
        return _contacts_redirect(f"Saved {contact.name}")

    if contact is None:
        rows = [_blank_channel_row()]
        fields = {
            "name": "",
            "nickname": "",
            "birth_year": "",
            "household": request.GET.get("household", ""),
            "notes": "",
            "tags": "",
        }
    else:
        active = contact.channels.filter(status=ContactChannel.Status.ACTIVE).order_by("id")
        rows = [_channel_row_from(c) for c in active] or [_blank_channel_row()]
        fields = _contact_fields_from(contact)
    return render(
        request, "core/contact_form.html", _contact_form_context(contact, rows=rows, fields=fields)
    )


def _posted_member_rows(request) -> list[dict]:
    """Parse the new-household form's parallel-array member inputs. A row counts as a
    member when it has a name and isn't delete-flagged."""
    names = request.POST.getlist("member_name")
    nicks = request.POST.getlist("member_nick")
    births = request.POST.getlist("member_birth")
    kinds = request.POST.getlist("member_ch_kind")
    values = request.POST.getlist("member_ch_value")
    deletes = request.POST.getlist("member_delete")
    rows = []
    for i, raw_name in enumerate(names):
        if (deletes[i] if i < len(deletes) else "0") == "1":
            continue
        name = raw_name.strip()[:MAX_NAME_LEN]
        if not name:
            continue
        rows.append(
            {
                "name": name,
                "nick": (nicks[i] if i < len(nicks) else "").strip()[:MAX_NICK_LEN],
                "birth": (births[i] if i < len(births) else "").strip(),
                "kind": (kinds[i] if i < len(kinds) else "").strip(),
                "value": (values[i] if i < len(values) else "").strip()[:254],
            }
        )
    return rows


@staff_member_required
@require_http_methods(["GET", "POST"])
def household_new(request):
    """Create a household + its members + one contact method each, all in one submit
    (§2.2) — the thing the Django admin can't do (the members and their channels in a
    single form)."""
    if request.method == "POST":
        name = request.POST.get("name", "").strip()[:MAX_NAME_LEN]
        members = _posted_member_rows(request)
        error = None
        if not name:
            error = "A household needs a name."
        elif not members:
            error = "Add at least one member (with a name)."
        else:
            for m in members:
                if m["kind"]:
                    _, err = validate_channel_value(m["kind"], m["value"])
                    if err:
                        error = f"{m['name']}: {err}"
                        break
        if error:
            return render(
                request,
                "core/household_new.html",
                {
                    "kind_choices": CHANNEL_KIND_CHOICES,
                    "messenger_kind": ContactChannel.Kind.MESSENGER,
                    "name": name,
                    "members": members or [{}, {}],
                    "error": error,
                },
            )
        with transaction.atomic():
            household = Household.objects.create(name=name, created_by=request.user)
            for m in members:
                contact = Contact.objects.create(
                    name=m["name"],
                    nickname=m["nick"],
                    birth_year=_parse_year(m["birth"]),
                    household=household,
                    created_by=request.user,
                )
                if m["kind"]:
                    value, _ = validate_channel_value(m["kind"], m["value"])
                    ContactChannel.objects.create(
                        contact=contact,
                        kind=m["kind"],
                        value=value,
                        is_preferred=True,
                        source=ContactChannel.Source.ORGANIZER,
                        status=ContactChannel.Status.ACTIVE,
                    )
        return _contacts_redirect(f"Created household {household.name} ({len(members)} members)")

    return render(
        request,
        "core/household_new.html",
        {
            "kind_choices": CHANNEL_KIND_CHOICES,
            "messenger_kind": ContactChannel.Kind.MESSENGER,
            "name": "",
            "members": [{}, {}],
            "error": None,
        },
    )


@staff_member_required
@require_http_methods(["GET", "POST"])
def household_edit(request, pk):
    """Rename a household (§2.2). Membership itself is edited via each member's contact
    page — a contact's `household` field."""
    household = get_object_or_404(Household.objects.prefetch_related("members"), pk=pk)
    if request.method == "POST":
        name = request.POST.get("name", "").strip()[:MAX_NAME_LEN]
        if not name:
            return render(
                request,
                "core/household_edit.html",
                {"household": household, "error": "A household needs a name."},
            )
        household.name = name
        household.save(update_fields=["name", "updated_at"])
        return _contacts_redirect(f"Updated household {household.name}")
    return render(request, "core/household_edit.html", {"household": household, "error": None})


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

    # bounced/complained: provider accepted then the message failed to land, or the
    # recipient marked it spam. failed: an async delivery failure after acceptance —
    # the synchronous send path (channels.py) only catches API-time rejection, so
    # without this the Delivery would sit at SENT forever.
    if payload.get("type") in ("email.bounced", "email.complained", "email.failed"):
        data = payload.get("data") or {}
        provider_id = data.get("email_id") or data.get("id") or ""
        delivery = (
            Delivery.objects.select_related("invitation")
            .filter(provider_message_id=provider_id)
            .first()
        )
        if delivery:
            failed = payload["type"] == "email.failed"
            delivery.status = Delivery.Status.FAILED if failed else Delivery.Status.BOUNCED
            # Keep the provider's human reason (why it bounced) for the dashboard, not
            # just the event name — e.g. "the account does not exist".
            bounce = data.get("bounce") or {}
            delivery.error = (bounce.get("message") or payload["type"])[:500]
            delivery.save(update_fields=["status", "error", "updated_at"])
            # Ladder rules apply: this can't override an open/response (§2.3).
            delivery.invitation.advance_state(Invitation.State.BOUNCED)
    return JsonResponse({"ok": True})
