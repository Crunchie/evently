from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import (
    Contact,
    ContactChannel,
    Event,
    Household,
    Invitation,
    InvitationAttendee,
)

Rsvp = InvitationAttendee.Rsvp


def make_event():
    return Event.objects.create(
        title="Party",
        starts_at=timezone.now() + timedelta(days=7),
        status=Event.Status.ACTIVE,
    )


@pytest.mark.django_db
def test_invitation_requires_contact_xor_household():
    e = make_event()
    c = Contact.objects.create(name="Alex")
    h = Household.objects.create(name="The Hendersons")

    with pytest.raises(IntegrityError):  # both set
        with transaction.atomic():
            Invitation.objects.create(event=e, contact=c, household=h)

    with pytest.raises(IntegrityError):  # neither set
        with transaction.atomic():
            Invitation.objects.create(event=e)

    assert Invitation.objects.create(event=e, contact=c).pk  # exactly one -> ok


@pytest.mark.django_db
def test_no_double_invite_same_contact():
    e = make_event()
    c = Contact.objects.create(name="Alex")
    Invitation.objects.create(event=e, contact=c)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Invitation.objects.create(event=e, contact=c)


@pytest.mark.django_db
def test_one_preferred_channel_per_contact():
    c = Contact.objects.create(name="Alex")
    ContactChannel.objects.create(
        contact=c, kind=ContactChannel.Kind.EMAIL, value="a@x.com", is_preferred=True
    )
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            ContactChannel.objects.create(
                contact=c, kind=ContactChannel.Kind.WHATSAPP, value="+15550001", is_preferred=True
            )
    # a second *non-preferred* channel is fine
    assert ContactChannel.objects.create(
        contact=c, kind=ContactChannel.Kind.WHATSAPP, value="+15550001"
    ).pk


@pytest.mark.django_db
def test_one_attendee_row_per_person():
    e = make_event()
    c = Contact.objects.create(name="Alex")
    inv = Invitation.objects.create(event=e, contact=c)
    assert inv.attendees.count() == 1  # auto-created on save
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            InvitationAttendee.objects.create(invitation=inv, contact=c)


@pytest.mark.django_db
def test_attendee_rows_auto_created_and_synced():
    e = make_event()
    hh = Household.objects.create(name="Hendersons")
    Contact.objects.create(name="Jane", household=hh)
    Contact.objects.create(name="Mark", household=hh)
    inv = Invitation.objects.create(event=e, household=hh)
    assert set(inv.attendees.values_list("contact__name", flat=True)) == {"Jane", "Mark"}

    inv.sync_attendees()  # idempotent
    assert inv.attendees.count() == 2

    # a member added to the household later is picked up on re-sync
    baby = Contact.objects.create(name="Baby", household=hh)
    inv.sync_attendees()
    assert inv.attendees.count() == 3

    # leaving the household never removes history rows
    baby.household = None
    baby.save()
    inv.sync_attendees()
    assert inv.attendees.count() == 3


@pytest.mark.django_db
def test_expected_headcount():
    e = make_event()

    # single contact, Going, +1
    c1 = Contact.objects.create(name="Alex")
    inv1 = Invitation.objects.create(event=e, contact=c1, plus_ones=1)
    inv1.attendees.update(rsvp_status=Rsvp.GOING)

    # household: 2 Going, 1 Can't, +2
    hh = Household.objects.create(name="Hendersons")
    c2 = Contact.objects.create(name="Jane", household=hh)
    c3 = Contact.objects.create(name="Mark", household=hh)
    c4 = Contact.objects.create(name="Mia", household=hh)
    inv2 = Invitation.objects.create(event=e, household=hh, plus_ones=2)
    inv2.attendees.filter(contact__in=[c2, c3]).update(rsvp_status=Rsvp.GOING)
    inv2.attendees.filter(contact=c4).update(rsvp_status=Rsvp.CANT)

    # single contact, Maybe, +5 -> plus-ones excluded (no Going attendee on this envelope)
    c5 = Contact.objects.create(name="Sam")
    inv3 = Invitation.objects.create(event=e, contact=c5, plus_ones=5)
    inv3.attendees.update(rsvp_status=Rsvp.MAYBE)

    # Going attendees: c1, c2, c3 = 3 ; plus-ones from inv1(1)+inv2(2) = 3 ; total 6
    assert e.expected_headcount == 6


@pytest.mark.django_db
def test_state_ladder_is_monotonic():
    e = make_event()
    c = Contact.objects.create(name="Alex")
    inv = Invitation.objects.create(event=e, contact=c)

    assert inv.advance_state(Invitation.State.SENT)
    assert inv.advance_state(Invitation.State.OPENED)
    assert not inv.advance_state(Invitation.State.SENT)  # resend can't regress
    assert inv.advance_state(Invitation.State.RESPONDED)
    assert not inv.advance_state(Invitation.State.OPENED)  # click after responding
    inv.refresh_from_db()
    assert inv.state == Invitation.State.RESPONDED


@pytest.mark.django_db
def test_bounce_only_before_open_and_open_clears_bounce():
    e = make_event()
    c = Contact.objects.create(name="Alex")
    inv = Invitation.objects.create(event=e, contact=c)

    inv.advance_state(Invitation.State.SENT)
    assert inv.advance_state(Invitation.State.BOUNCED)  # bounce after send applies
    assert inv.advance_state(Invitation.State.OPENED)  # a later open clears it

    c2 = Contact.objects.create(name="Priya")
    inv2 = Invitation.objects.create(event=e, contact=c2)
    inv2.advance_state(Invitation.State.SENT)
    inv2.advance_state(Invitation.State.OPENED)
    assert not inv2.advance_state(Invitation.State.BOUNCED)  # can't override an open


@pytest.mark.django_db
def test_revoked_is_terminal():
    e = make_event()
    c = Contact.objects.create(name="Alex")
    inv = Invitation.objects.create(event=e, contact=c)

    assert inv.advance_state(Invitation.State.REVOKED)
    assert not inv.advance_state(Invitation.State.OPENED)
    assert not inv.advance_state(Invitation.State.RESPONDED)
    inv.refresh_from_db()
    assert inv.state == Invitation.State.REVOKED
