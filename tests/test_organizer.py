"""Phase 6: organizer RSVP override (§2.3), per-guest row actions, the
channel-change request → approval loop (§2.5), and the day-before reminder (§2.4)."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core import channels
from core.models import (
    Contact,
    ContactChannel,
    Event,
    Household,
    Invitation,
    InvitationAttendee,
    RsvpEvent,
)

Kind = ContactChannel.Kind
Rsvp = InvitationAttendee.Rsvp
State = Invitation.State


@pytest.fixture
def staff_client(client, django_user_model):
    user = django_user_model.objects.create_superuser("sam", "sam@example.com", "pw-strong-123")
    client.force_login(user)
    return client


@pytest.fixture
def fake_send(monkeypatch):
    sent_batches = []

    def _fake(messages):
        sent_batches.append(messages)
        return [f"re_{i}" for i in range(len(messages))]

    monkeypatch.setattr(channels, "send_email_batch", _fake)
    return sent_batches


@pytest.fixture
def event(db):
    return Event.objects.create(
        title="Summer BBQ",
        starts_at=timezone.now() + timedelta(days=7),
        status=Event.Status.ACTIVE,
        host_display="Sam & Kate",
    )


def contact_with_email(name, email, preferred=True):
    contact = Contact.objects.create(name=name)
    ContactChannel.objects.create(
        contact=contact, kind=Kind.EMAIL, value=email, is_preferred=preferred
    )
    return contact


# --------------------------------------------------------------------------- #
#  Organizer override (§2.3)
# --------------------------------------------------------------------------- #
def test_override_records_organizer_actor(staff_client, event, django_user_model):
    inv = Invitation.objects.create(event=event, contact=Contact.objects.create(name="Dave"))
    attendee = inv.attendees.get()

    resp = staff_client.post(
        reverse("invitation-override", args=[inv.pk]),
        {f"status_{attendee.pk}": "going", "plus_ones": "2", "note": "told me at football"},
    )
    assert resp.status_code == 302

    attendee.refresh_from_db()
    inv.refresh_from_db()
    assert attendee.rsvp_status == Rsvp.GOING
    assert inv.plus_ones == 2 and inv.latest_note == "told me at football"
    assert inv.state == State.RESPONDED

    history = attendee.history.get()
    assert history.actor == RsvpEvent.Actor.ORGANIZER
    assert history.actor_user.username == "sam"


def test_override_can_return_to_no_reply_but_guests_cannot(staff_client, event):
    inv = Invitation.objects.create(event=event, contact=Contact.objects.create(name="Dave"))
    attendee = inv.attendees.get()
    staff_client.post(inv.rsvp_path, {f"status_{attendee.pk}": "going"})  # guest says going

    staff_client.post(
        reverse("invitation-override", args=[inv.pk]), {f"status_{attendee.pk}": "no_reply"}
    )
    attendee.refresh_from_db()
    assert attendee.rsvp_status == Rsvp.NO_REPLY and attendee.responded_at is None

    # the guest page ignores a no_reply write (not one of its radio values)
    staff_client.post(inv.rsvp_path, {f"status_{attendee.pk}": "no_reply"})
    attendee.refresh_from_db()
    assert attendee.rsvp_status == Rsvp.NO_REPLY  # unchanged, no new history row
    assert attendee.history.count() == 2  # guest going + organizer reset


def test_override_household_members_in_one_action(staff_client, event):
    hh = Household.objects.create(name="The Hendersons")
    for name in ("Jane", "Mark", "Kid"):
        Contact.objects.create(name=name, household=hh)
    inv = Invitation.objects.create(event=event, household=hh)
    jane, mark, kid = list(inv.attendees.order_by("id"))

    staff_client.post(
        reverse("invitation-override", args=[inv.pk]),
        {f"status_{jane.pk}": "going", f"status_{mark.pk}": "going", f"status_{kid.pk}": "cant"},
    )
    statuses = {a.contact.name: a.rsvp_status for a in inv.attendees.select_related("contact")}
    assert statuses == {"Jane": Rsvp.GOING, "Mark": Rsvp.GOING, "Kid": Rsvp.CANT}


# --------------------------------------------------------------------------- #
#  Row actions: revoke / regenerate / resend / nudge
# --------------------------------------------------------------------------- #
def test_revoke_kills_the_link_quietly(staff_client, client, event):
    inv = Invitation.objects.create(event=event, contact=Contact.objects.create(name="Dave"))
    staff_client.post(reverse("invitation-action", args=[inv.pk]), {"action": "revoke"})
    inv.refresh_from_db()
    assert inv.state == State.REVOKED
    resp = client.get(inv.rsvp_path)
    assert resp.status_code == 410
    assert "Summer BBQ" not in resp.content.decode()  # no details leak


def test_regenerate_rotates_the_token(staff_client, client, event):
    inv = Invitation.objects.create(event=event, contact=Contact.objects.create(name="Dave"))
    old_path = inv.rsvp_path
    staff_client.post(reverse("invitation-action", args=[inv.pk]), {"action": "regenerate"})
    inv.refresh_from_db()
    assert inv.rsvp_path != old_path
    assert client.get(old_path).status_code == 404
    assert client.get(inv.rsvp_path).status_code == 200


def test_single_guest_resend_and_nudge(staff_client, event, fake_send):
    inv = Invitation.objects.create(event=event, contact=contact_with_email("Dave", "d@x.com"))
    other = Invitation.objects.create(event=event, contact=contact_with_email("Ana", "a@x.com"))

    staff_client.post(reverse("invitation-action", args=[inv.pk]), {"action": "resend"})
    assert [m["to"] for m in fake_send[0]] == [["d@x.com"]]  # only Dave, not Ana

    staff_client.post(reverse("invitation-action", args=[inv.pk]), {"action": "nudge"})
    assert "hoping" in fake_send[1][0]["subject"]
    other.refresh_from_db()
    assert other.state == State.PENDING


# --------------------------------------------------------------------------- #
#  Channel change: guest request → dashboard approval (§2.5)
# --------------------------------------------------------------------------- #
def test_guest_requests_whatsapp_normalised_to_e164(client, event):
    inv = Invitation.objects.create(event=event, contact=Contact.objects.create(name="Dave"))
    resp = client.post(
        reverse("rsvp-channel", args=[inv.token]),
        {"kind": "whatsapp", "value": "021 123 4567"},
    )
    assert resp.status_code == 302 and "channel_requested=1" in resp["Location"]

    proposed = inv.contact.channels.get()
    assert proposed.status == ContactChannel.Status.PROPOSED
    assert proposed.source == ContactChannel.Source.GUEST
    assert proposed.value == "+64211234567"
    assert proposed.requested_via == inv
    # visible on the dashboard approval queue
    assert not proposed.is_preferred


def test_guest_request_validation_rejects_bad_input(client, event):
    inv = Invitation.objects.create(event=event, contact=Contact.objects.create(name="Dave"))
    for payload in (
        {"kind": "email", "value": "not-an-email"},
        {"kind": "whatsapp", "value": "12"},
        {"kind": "sms", "value": "+64211234567"},  # unsupported kind
    ):
        resp = client.post(reverse("rsvp-channel", args=[inv.token]), payload)
        assert "channel_error=1" in resp["Location"]
    assert not inv.contact.channels.exists()


def test_guest_new_request_replaces_pending_one(client, event):
    inv = Invitation.objects.create(event=event, contact=Contact.objects.create(name="Dave"))
    client.post(reverse("rsvp-channel", args=[inv.token]), {"kind": "messenger", "value": ""})
    client.post(reverse("rsvp-channel", args=[inv.token]), {"kind": "email", "value": "d@x.com"})
    proposed = inv.contact.channels.get()  # only one pending
    assert proposed.kind == Kind.EMAIL and proposed.value == "d@x.com"


def test_household_request_targets_chosen_member(client, event):
    hh = Household.objects.create(name="The Hendersons")
    jane = Contact.objects.create(name="Jane", household=hh)
    mark = Contact.objects.create(name="Mark", household=hh)
    inv = Invitation.objects.create(event=event, household=hh)

    client.post(
        reverse("rsvp-channel", args=[inv.token]),
        {"kind": "email", "value": "mark@x.com", "member": mark.pk},
    )
    assert mark.channels.filter(status=ContactChannel.Status.PROPOSED).exists()
    assert not jane.channels.exists()
    # a member id outside the household is rejected
    outsider = Contact.objects.create(name="Sneaky")
    resp = client.post(
        reverse("rsvp-channel", args=[inv.token]),
        {"kind": "email", "value": "s@x.com", "member": outsider.pk},
    )
    assert "channel_error=1" in resp["Location"] and not outsider.channels.exists()


def test_approve_makes_channel_active_and_preferred(staff_client, event):
    contact = contact_with_email("Dave", "d@x.com")  # currently preferred: email
    inv = Invitation.objects.create(event=event, contact=contact)
    proposed = ContactChannel.objects.create(
        contact=contact,
        kind=Kind.WHATSAPP,
        value="+64211234567",
        status=ContactChannel.Status.PROPOSED,
        source=ContactChannel.Source.GUEST,
        requested_via=inv,
    )
    resp = staff_client.post(
        reverse("channel-request-action", args=[proposed.pk]),
        {"action": "approve", "event": event.pk},
    )
    assert resp.status_code == 302

    proposed.refresh_from_db()
    assert proposed.status == ContactChannel.Status.ACTIVE and proposed.is_preferred
    assert not contact.channels.get(kind=Kind.EMAIL).is_preferred  # displaced
    # future sends now route assisted
    assert channels.route_channel(contact).kind == Kind.WHATSAPP


def test_reject_deletes_the_request(staff_client, event):
    contact = Contact.objects.create(name="Dave")
    proposed = ContactChannel.objects.create(
        contact=contact,
        kind=Kind.MESSENGER,
        status=ContactChannel.Status.PROPOSED,
        source=ContactChannel.Source.GUEST,
    )
    staff_client.post(
        reverse("channel-request-action", args=[proposed.pk]),
        {"action": "reject", "event": event.pk},
    )
    assert not contact.channels.exists()


def test_channel_actions_require_staff(client, db, event):
    contact = Contact.objects.create(name="Dave")
    proposed = ContactChannel.objects.create(
        contact=contact, kind=Kind.MESSENGER, status=ContactChannel.Status.PROPOSED
    )
    resp = client.post(reverse("channel-request-action", args=[proposed.pk]), {"action": "approve"})
    assert resp.status_code == 302 and "/admin/login" in resp["Location"]
    proposed.refresh_from_db()
    assert proposed.status == ContactChannel.Status.PROPOSED


# --------------------------------------------------------------------------- #
#  Day-before reminder (§2.4) + dashboard streams (§2.6)
# --------------------------------------------------------------------------- #
def test_reminder_goes_to_going_and_maybe_only(staff_client, event, fake_send):
    def invitee(name, email, status):
        inv = Invitation.objects.create(event=event, contact=contact_with_email(name, email))
        attendee = inv.attendees.get()
        if status:
            staff_client.post(inv.rsvp_path, {f"status_{attendee.pk}": status})
        return inv

    invitee("Yes", "yes@x.com", "going")
    invitee("Perhaps", "maybe@x.com", "maybe")
    invitee("No", "no@x.com", "cant")
    invitee("Silent", "quiet@x.com", None)

    fake_send.clear()
    resp = staff_client.post(reverse("event-send", args=[event.pk]), {"action": "reminder"})
    assert "sent=2" in resp["Location"]
    recipients = {m["to"][0] for m in fake_send[0]}
    assert recipients == {"yes@x.com", "maybe@x.com"}
    assert "See you" in fake_send[0][0]["subject"]


def test_dashboard_prompts_reminder_near_event(staff_client, event):
    url = reverse("event-dashboard", args=[event.pk])
    assert "day-before reminder" not in staff_client.get(url).content.decode().lower()
    event.starts_at = timezone.now() + timedelta(hours=20)
    event.save()
    assert "reminder" in staff_client.get(url).content.decode().lower()


def test_dashboard_shows_streams_and_approvals(staff_client, event):
    contact = Contact.objects.create(name="Dave")
    inv = Invitation.objects.create(event=event, contact=contact)
    attendee = inv.attendees.get()
    staff_client.post(inv.rsvp_path, {f"status_{attendee.pk}": "going", "note": "bringing pavlova"})
    ContactChannel.objects.create(
        contact=contact,
        kind=Kind.WHATSAPP,
        value="+64211234567",
        status=ContactChannel.Status.PROPOSED,
        source=ContactChannel.Source.GUEST,
        requested_via=inv,
    )
    page = staff_client.get(reverse("event-dashboard", args=[event.pk])).content.decode()
    assert "bringing pavlova" in page  # notes stream
    assert "Channel-change requests" in page and "+64211234567" in page  # approval queue
    assert "by guest" in page  # response history
