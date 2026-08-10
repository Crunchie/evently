"""Quality-pass regressions (2026-07-05 review): revoked envelopes leave every
count, redirect params are encoded, hostile input can't 500 the approval flow,
outbox rows only ever address covered contacts, and absent form fields don't
wipe notes."""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from django.urls import reverse
from django.utils import timezone

from core.channels import enqueue
from core.models import Contact, ContactChannel, Delivery, Event, Household, Invitation

Kind = ContactChannel.Kind
State = Invitation.State


@pytest.fixture
def staff_client(client, django_user_model):
    user = django_user_model.objects.create_superuser("sam", "sam@example.com", "pw-strong-123")
    client.force_login(user)
    return client


@pytest.fixture
def event(db):
    return Event.objects.create(
        title="Summer BBQ",
        starts_at=timezone.now() + timedelta(days=7),
        status=Event.Status.ACTIVE,
        show_guest_list=True,
    )


def invite(event, name, status=None, plus_ones=0):
    inv = Invitation.objects.create(
        event=event, contact=Contact.objects.create(name=name), plus_ones=plus_ones
    )
    if status:
        attendee = inv.attendees.get()
        attendee.rsvp_status = status
        attendee.save(update_fields=["rsvp_status"])
    return inv


# --------------------------------------------------------------------------- #
#  Revoked envelopes leave every count (§2.2 uninvite)
# --------------------------------------------------------------------------- #
def test_revoked_guest_leaves_headcount_and_stats(staff_client, event):
    invite(event, "Stays", status="going", plus_ones=1)
    revoked = invite(event, "Uninvited", status="going", plus_ones=2)
    assert event.expected_headcount == 5  # both counted before the uninvite

    staff_client.post(reverse("invitation-action", args=[revoked.pk]), {"action": "revoke"})
    assert event.expected_headcount == 2  # Stays + their plus-one only

    page = staff_client.get(reverse("event-dashboard", args=[event.pk])).content.decode()
    assert '>2</div><div class="l">Total expected' in page.replace("\n", "")


def test_revoked_guest_never_shows_on_the_guest_list(client, staff_client, event):
    visible = invite(event, "Alice", status="going")
    revoked = invite(event, "Bob", status="going")
    staff_client.post(reverse("invitation-action", args=[revoked.pk]), {"action": "revoke"})

    page = client.get(visible.rsvp_path).content.decode()
    assert "Alice" in page and "Bob" not in page


# --------------------------------------------------------------------------- #
#  Redirect hygiene
# --------------------------------------------------------------------------- #
def test_dashboard_redirect_params_are_encoded(staff_client, event):
    contact = Contact.objects.create(name="A&B=Café")
    inv = Invitation.objects.create(event=event, contact=contact)
    proposed = ContactChannel.objects.create(
        contact=contact,
        kind=Kind.MESSENGER,
        status=ContactChannel.Status.PROPOSED,
        source=ContactChannel.Source.GUEST,
        requested_via=inv,
    )
    resp = staff_client.post(
        reverse("channel-request-action", args=[proposed.pk]),
        {"action": "approve", "event": event.pk},
    )
    query = parse_qs(urlparse(resp["Location"]).query)
    # the hostile name stayed inside one parameter instead of splitting the query
    assert query["msg"] == ["A&B=Café → Messenger"]
    assert staff_client.get(resp["Location"]).status_code == 200


def test_channel_action_survives_garbage_event_pk(staff_client, event):
    contact = Contact.objects.create(name="Dave")
    inv = Invitation.objects.create(event=event, contact=contact)
    proposed = ContactChannel.objects.create(
        contact=contact,
        kind=Kind.MESSENGER,
        status=ContactChannel.Status.PROPOSED,
        source=ContactChannel.Source.GUEST,
        requested_via=inv,
    )
    # falls back to the requesting invitation's event instead of 500ing
    resp = staff_client.post(
        reverse("channel-request-action", args=[proposed.pk]),
        {"action": "approve", "event": "junk"},
    )
    assert resp.status_code == 302 and f"/admin/events/{event.pk}/" in resp["Location"]

    orphan = ContactChannel.objects.create(
        contact=contact, kind=Kind.MESSENGER, status=ContactChannel.Status.PROPOSED
    )
    resp = staff_client.post(
        reverse("channel-request-action", args=[orphan.pk]), {"action": "reject", "event": ""}
    )
    assert resp.status_code == 403  # no event derivable anywhere — refuse, don't crash


# --------------------------------------------------------------------------- #
#  Queue integrity: a row can only ever address someone the envelope covers.
#
#  The old queue took an (invitation, channel) pair straight from the POST, so a forged
#  channel id had to be rejected at share time. Rows are now addressed server-side by
#  enqueue() and actioned by their own pk, so the whole class of forgery is gone — these
#  guard the property that replaced the check.
# --------------------------------------------------------------------------- #
def test_enqueue_never_routes_to_another_contacts_channel(staff_client, event):
    mine = Contact.objects.create(name="Mine")
    ContactChannel.objects.create(
        contact=mine, kind=Kind.WHATSAPP, value="+64211234567", is_preferred=True
    )
    inv = Invitation.objects.create(event=event, contact=mine)
    other = Contact.objects.create(name="Other")
    ContactChannel.objects.create(
        contact=other, kind=Kind.WHATSAPP, value="+64222222222", is_preferred=True
    )

    enqueue([inv], "invite", "http://testserver")
    assert [d.address_used for d in inv.deliveries.all()] == ["+64211234567"]


def test_enqueue_uses_household_members_channels(staff_client, event):
    hh = Household.objects.create(name="The Hendersons")
    member = Contact.objects.create(name="Jane", household=hh)
    ContactChannel.objects.create(
        contact=member, kind=Kind.WHATSAPP, value="+64211234567", is_preferred=True
    )
    inv = Invitation.objects.create(event=event, household=hh)

    enqueue([inv], "invite", "http://testserver")
    delivery = inv.deliveries.get()
    assert delivery.address_used == "+64211234567"
    assert delivery.channel.contact_id == member.pk


def test_marking_a_row_sent_only_touches_that_row(staff_client, event):
    a = Contact.objects.create(name="A")
    ContactChannel.objects.create(
        contact=a, kind=Kind.WHATSAPP, value="+64211111111", is_preferred=True
    )
    b = Contact.objects.create(name="B")
    ContactChannel.objects.create(
        contact=b, kind=Kind.WHATSAPP, value="+64222222222", is_preferred=True
    )
    inv_a = Invitation.objects.create(event=event, contact=a)
    inv_b = Invitation.objects.create(event=event, contact=b)
    enqueue([inv_a, inv_b], "invite", "http://testserver")

    staff_client.post(
        reverse("message-action", args=[inv_a.deliveries.get().pk]), {"action": "mark_sent"}
    )
    assert inv_a.deliveries.get().status == Delivery.Status.SENT
    assert inv_b.deliveries.get().status == Delivery.Status.PENDING


# --------------------------------------------------------------------------- #
#  Notes survive forms that don't carry the field
# --------------------------------------------------------------------------- #
def test_override_without_note_field_keeps_guest_note(staff_client, event):
    inv = invite(event, "Dave")
    attendee = inv.attendees.get()
    staff_client.post(inv.rsvp_path, {f"status_{attendee.pk}": "going", "note": "bringing pavlova"})

    staff_client.post(
        reverse("invitation-override", args=[inv.pk]), {f"status_{attendee.pk}": "maybe"}
    )
    inv.refresh_from_db()
    assert inv.latest_note == "bringing pavlova"  # untouched — field wasn't submitted

    # ...but an explicit empty note still clears it
    staff_client.post(
        reverse("invitation-override", args=[inv.pk]),
        {f"status_{attendee.pk}": "cant", "note": ""},
    )
    inv.refresh_from_db()
    assert inv.latest_note == ""
