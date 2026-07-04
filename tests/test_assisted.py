"""Phase 5: channel routing, wa.me deep links, and the assisted send queue (§6).
No network anywhere — assisted channels are share payloads + optimistic SHARED rows."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.channels import assisted_channels, email_channels, route_channel, wa_link
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
        status=Event.Status.ACTIVE,  # guests can respond (nudge tests post RSVPs)
        host_display="Sam & Kate",
    )


def make_contact(name, *channels):
    """channels: (kind, value, is_preferred) tuples."""
    contact = Contact.objects.create(name=name)
    for kind, value, preferred in channels:
        ContactChannel.objects.create(
            contact=contact, kind=kind, value=value, is_preferred=preferred
        )
    return contact


def queue_url(event, **params):
    url = reverse("event-queue", args=[event.pk])
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{url}?{query}" if query else url


# --------------------------------------------------------------------------- #
#  Routing (§2.2): preferred wins; else email > WhatsApp > Messenger
# --------------------------------------------------------------------------- #
def test_preferred_assisted_channel_beats_email(db, event):
    contact = make_contact(
        "Dave", (Kind.EMAIL, "d@x.com", False), (Kind.WHATSAPP, "+64211234567", True)
    )
    inv = Invitation.objects.create(event=event, contact=contact)
    assert route_channel(contact).kind == Kind.WHATSAPP
    assert email_channels(inv) == []  # not emailed — he asked for WhatsApp
    assert [ch.kind for ch in assisted_channels(inv)] == [Kind.WHATSAPP]


def test_fallback_order_email_whatsapp_messenger(db):
    no_pref = make_contact(
        "Ana", (Kind.MESSENGER, "", False), (Kind.WHATSAPP, "+64211234567", False)
    )
    assert route_channel(no_pref).kind == Kind.WHATSAPP  # direct targeting beats share sheet
    with_email = make_contact("Bea", (Kind.MESSENGER, "", False), (Kind.EMAIL, "b@x.com", False))
    assert route_channel(with_email).kind == Kind.EMAIL  # automated beats assisted
    assert route_channel(Contact.objects.create(name="Cal")) is None


def test_unusable_channels_never_route(db):
    # SMS has no transport yet; a valueless WhatsApp can't build a link.
    contact = make_contact("Eve", (Kind.SMS, "+64211234567", True), (Kind.WHATSAPP, "", False))
    assert route_channel(contact) is None


# --------------------------------------------------------------------------- #
#  wa.me links (§6)
# --------------------------------------------------------------------------- #
def test_wa_link_normalises_local_numbers(settings):
    settings.PHONE_REGION = "NZ"
    link = wa_link("021 123 4567", "Hi Dave — you're invited!")
    assert link.startswith("https://wa.me/64211234567?text=")
    assert "Hi%20Dave" in link
    assert wa_link("+64 21 123 4567", "hi") == wa_link("021 123 4567", "hi")


def test_wa_link_rejects_garbage():
    assert wa_link("not a phone", "hi") is None
    assert wa_link("12", "hi") is None


# --------------------------------------------------------------------------- #
#  The send queue: share → next (§6)
# --------------------------------------------------------------------------- #
def test_queue_walk_share_and_done(staff_client, event):
    dave = make_contact("Dave", (Kind.WHATSAPP, "+64211234567", True))
    ana = make_contact("Ana", (Kind.MESSENGER, "", True))
    inv_dave = Invitation.objects.create(event=event, contact=dave)
    inv_ana = Invitation.objects.create(event=event, contact=ana)

    page = staff_client.get(queue_url(event, kind="invite")).content.decode()
    assert "1 of 2" in page and "Dave" in page and "wa.me/64211234567" in page
    assert inv_dave.rsvp_path in page  # the link rides inside the message text

    resp = staff_client.post(
        reverse("event-queue", args=[event.pk]),
        {
            "action": "shared",
            "kind": "invite",
            "n": 0,
            "invitation": inv_dave.pk,
            "channel": dave.channels.get().pk,
        },
    )
    assert resp.status_code == 302 and "n=0" in resp["Location"]  # list shrank under n

    inv_dave.refresh_from_db()
    assert inv_dave.state == State.SHARED
    delivery = inv_dave.deliveries.get()
    assert delivery.status == Delivery.Status.SHARED and delivery.kind == Kind.WHATSAPP

    page = staff_client.get(queue_url(event, kind="invite")).content.decode()
    assert "1 of 1" in page and "Ana" in page and "Share" in page  # messenger card

    staff_client.post(
        reverse("event-queue", args=[event.pk]),
        {
            "action": "shared",
            "kind": "invite",
            "n": 0,
            "invitation": inv_ana.pk,
            "channel": ana.channels.get().pk,
        },
    )
    page = staff_client.get(queue_url(event, kind="invite")).content.decode()
    assert "Queue done" in page


def test_queue_skip_moves_on_without_delivery(staff_client, event):
    dave = make_contact("Dave", (Kind.WHATSAPP, "+64211234567", True))
    inv = Invitation.objects.create(event=event, contact=dave)

    resp = staff_client.post(
        reverse("event-queue", args=[event.pk]),
        {
            "action": "skip",
            "kind": "invite",
            "n": 0,
            "invitation": inv.pk,
            "channel": dave.channels.get().pk,
        },
    )
    assert "n=1" in resp["Location"]
    inv.refresh_from_db()
    assert inv.state == State.PENDING and not inv.deliveries.exists()
    assert "Queue done" in staff_client.get(queue_url(event, kind="invite", n=1)).content.decode()


def test_household_two_whatsapp_parents_two_taps_same_link(staff_client, event):
    hh = Household.objects.create(name="The Hendersons")
    for name, phone in (("Jane", "+64211111111"), ("Mark", "+64222222222")):
        contact = make_contact(name, (Kind.WHATSAPP, phone, True))
        contact.household = hh
        contact.save()
    inv = Invitation.objects.create(event=event, household=hh)

    items_page = staff_client.get(queue_url(event, kind="invite")).content.decode()
    assert "1 of 2" in items_page

    # Share to parent one — the envelope must STAY queued for parent two.
    jane_channel = ContactChannel.objects.get(value="+64211111111")
    staff_client.post(
        reverse("event-queue", args=[event.pk]),
        {
            "action": "shared",
            "kind": "invite",
            "n": 0,
            "invitation": inv.pk,
            "channel": jane_channel.pk,
        },
    )
    page = staff_client.get(queue_url(event, kind="invite")).content.decode()
    assert "1 of 1" in page and "wa.me/64222222222" in page
    # both deliveries reference the same envelope → same link
    assert inv.deliveries.count() == 1
    assert inv.rsvp_path in page


def test_mixed_household_splits_email_and_queue(db, event):
    hh = Household.objects.create(name="Mixed")
    emailer = make_contact("Jane", (Kind.EMAIL, "jane@x.com", True))
    sharer = make_contact("Mark", (Kind.WHATSAPP, "+64222222222", True))
    for c in (emailer, sharer):
        c.household = hh
        c.save()
    inv = Invitation.objects.create(event=event, household=hh)
    assert [ch.value for ch in email_channels(inv)] == ["jane@x.com"]
    assert [ch.value for ch in assisted_channels(inv)] == ["+64222222222"]


def test_nudge_queue_targets_only_nonresponders(staff_client, event):
    quiet = Invitation.objects.create(
        event=event, contact=make_contact("Quiet", (Kind.WHATSAPP, "+64211234567", True))
    )
    replied = Invitation.objects.create(
        event=event, contact=make_contact("Fast", (Kind.WHATSAPP, "+64222222222", True))
    )
    for inv in (quiet, replied):
        inv.advance_state(State.SHARED)
    attendee = replied.attendees.get()
    staff_client.post(replied.rsvp_path, {f"status_{attendee.pk}": "going"})

    page = staff_client.get(queue_url(event, kind="nudge")).content.decode()
    assert "1 of 1" in page and "Quiet" in page and "Fast" not in page


def test_queue_requires_staff(client, event):
    assert client.get(queue_url(event, kind="invite")).status_code == 302  # login redirect


def test_queue_rejects_unknown_kind(staff_client, event):
    assert staff_client.get(queue_url(event, kind="hax")).status_code == 403
