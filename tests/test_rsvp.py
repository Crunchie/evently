"""Guest RSVP page (§2.5): states, submits, history, headcount effects, ICS."""

from datetime import timedelta

import pytest
from django.utils import timezone

from core.models import Contact, Event, Household, Invitation, InvitationAttendee, RsvpEvent

Rsvp = InvitationAttendee.Rsvp
State = Invitation.State


def make_event(**overrides):
    defaults = {
        "title": "Summer BBQ",
        "starts_at": timezone.now() + timedelta(days=7),
        "status": Event.Status.ACTIVE,
        "location_text": "42 Maple Avenue",
        "host_display": "Sam & Kate",
    }
    defaults.update(overrides)
    return Event.objects.create(**defaults)


def single_invitation(event, name="Alex Doe"):
    contact = Contact.objects.create(name=name)
    return Invitation.objects.create(event=event, contact=contact)


@pytest.fixture
def event(db):
    return make_event()


# ---------------------------------------------------------------- page states
def test_fresh_page_renders_and_marks_opened(client, event):
    inv = single_invitation(event)
    resp = client.get(inv.rsvp_path)
    assert resp.status_code == 200
    content = resp.content.decode()
    assert "Summer BBQ" in content
    assert "Hi Alex" in content
    assert resp["Referrer-Policy"] == "no-referrer"

    inv.refresh_from_db()
    assert inv.opened_at is not None
    assert inv.state == State.OPENED


def test_opened_at_is_first_click_only(client, event):
    inv = single_invitation(event)
    client.get(inv.rsvp_path)
    inv.refresh_from_db()
    first = inv.opened_at
    client.get(inv.rsvp_path)
    inv.refresh_from_db()
    assert inv.opened_at == first


def test_unknown_token_404(client, db):
    assert client.get("/i/not-a-real-token").status_code == 404


def test_revoked_page_leaks_nothing(client, event):
    inv = single_invitation(event)
    inv.advance_state(State.REVOKED)
    resp = client.get(inv.rsvp_path)
    assert resp.status_code == 410
    content = resp.content.decode()
    assert "no longer available" in content
    assert "Summer BBQ" not in content  # no event details on the dead-end


def test_cancelled_event_shows_banner_and_blocks_post(client, event):
    inv = single_invitation(event)
    attendee = inv.attendees.get()
    event.status = Event.Status.CANCELLED
    event.save()

    resp = client.get(inv.rsvp_path)
    assert "cancelled" in resp.content.decode().lower()

    resp = client.post(inv.rsvp_path, {f"status_{attendee.pk}": "going"})
    assert resp.status_code == 403
    attendee.refresh_from_db()
    assert attendee.rsvp_status == Rsvp.NO_REPLY


def test_past_event_is_read_only(client, db):
    event = make_event(starts_at=timezone.now() - timedelta(days=1))
    inv = single_invitation(event)
    attendee = inv.attendees.get()

    resp = client.get(inv.rsvp_path)
    assert "ended" in resp.content.decode().lower()

    resp = client.post(inv.rsvp_path, {f"status_{attendee.pk}": "going"})
    assert resp.status_code == 403


# ---------------------------------------------------------------- submitting
def test_single_guest_rsvp_flow(client, event):
    inv = single_invitation(event)
    attendee = inv.attendees.get()

    resp = client.post(
        inv.rsvp_path,
        {f"status_{attendee.pk}": "going", "note": "bringing a pavlova", "plus_ones": "2"},
    )
    assert resp.status_code == 302 and "saved=1" in resp["Location"]

    attendee.refresh_from_db()
    inv.refresh_from_db()
    assert attendee.rsvp_status == Rsvp.GOING
    assert attendee.responded_at is not None
    assert inv.state == State.RESPONDED
    assert inv.plus_ones == 2
    assert inv.latest_note == "bringing a pavlova"

    history = RsvpEvent.objects.filter(attendee=attendee)
    assert history.count() == 1
    entry = history.get()
    assert entry.actor == RsvpEvent.Actor.GUEST and entry.status == Rsvp.GOING

    # changing the answer appends history, never overwrites
    client.post(inv.rsvp_path, {f"status_{attendee.pk}": "cant", "note": ""})
    attendee.refresh_from_db()
    assert attendee.rsvp_status == Rsvp.CANT
    assert RsvpEvent.objects.filter(attendee=attendee).count() == 2

    inv.refresh_from_db()
    assert inv.state == State.RESPONDED  # re-render/open can't regress it either


def test_household_per_member_rsvp(client, event):
    hh = Household.objects.create(name="The Hendersons")
    Contact.objects.create(name="Jane", household=hh)
    Contact.objects.create(name="Mark", household=hh)
    Contact.objects.create(name="Mia", household=hh)
    inv = Invitation.objects.create(event=event, household=hh)
    a = {att.contact.name: att for att in inv.attendees.select_related("contact")}

    resp = client.get(inv.rsvp_path)
    content = resp.content.decode()
    assert "Hi The Hendersons" in content
    for name in ("Jane", "Mark", "Mia"):
        assert name in content

    client.post(
        inv.rsvp_path,
        {
            f"status_{a['Jane'].pk}": "going",
            f"status_{a['Mark'].pk}": "going",
            f"status_{a['Mia'].pk}": "cant",
            "note": "Mark arriving late",
        },
    )
    inv.refresh_from_db()
    statuses = dict(inv.attendees.values_list("contact__name", "rsvp_status"))
    assert statuses == {"Jane": "going", "Mark": "going", "Mia": "cant"}
    assert inv.state == State.RESPONDED
    assert event.expected_headcount == 2
    # partial answers allowed: jane/mark/mia all answered here, but nothing forced them to


def test_plus_ones_clamped_and_toggleable(client, db):
    capped = make_event(plus_ones_cap=2)
    inv = single_invitation(capped)
    attendee = inv.attendees.get()
    client.post(inv.rsvp_path, {f"status_{attendee.pk}": "going", "plus_ones": "7"})
    inv.refresh_from_db()
    assert inv.plus_ones == 2  # clamped to the event cap

    no_plus = make_event(allow_plus_ones=False)
    inv2 = single_invitation(no_plus, name="Priya Patel")
    attendee2 = inv2.attendees.get()
    client.post(inv2.rsvp_path, {f"status_{attendee2.pk}": "going", "plus_ones": "5"})
    inv2.refresh_from_db()
    assert inv2.plus_ones == 0  # toggle off -> ignored


def test_guest_list_toggle(client, db):
    event = make_event(show_guest_list=True)
    going_inv = single_invitation(event, name="Priya Patel")
    going_att = going_inv.attendees.get()
    client.post(going_inv.rsvp_path, {f"status_{going_att.pk}": "going"})

    other = single_invitation(event, name="Tom Wilson")
    content = client.get(other.rsvp_path).content.decode()
    assert "Who's going" in content
    assert "Priya" in content and "Patel" not in content  # first names only (§2.1)

    hidden = make_event(show_guest_list=False)
    inv = single_invitation(hidden)
    assert "Who's going" not in client.get(inv.rsvp_path).content.decode()


# ---------------------------------------------------------------- calendar
def test_ics_download(client, event):
    inv = single_invitation(event)
    resp = client.get(f"{inv.rsvp_path}/calendar.ics")
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/calendar")
    body = resp.content.decode()
    assert f"UID:{event.ics_uid}@evently" in body
    assert "SUMMARY:Summer BBQ" in body
    assert "DTSTART:" in body and "DTEND:" in body


def test_ics_escapes_special_chars(client, db):
    event = make_event(title="Dinner; drinks, fun", location_text="1 Main St, Springfield")
    inv = single_invitation(event)
    body = client.get(f"{inv.rsvp_path}/calendar.ics").content.decode()
    assert "SUMMARY:Dinner\\; drinks\\, fun" in body
    assert "LOCATION:1 Main St\\, Springfield" in body


def test_ics_unavailable_when_revoked(client, event):
    inv = single_invitation(event)
    inv.advance_state(Invitation.State.REVOKED)
    assert client.get(f"{inv.rsvp_path}/calendar.ics").status_code == 410
