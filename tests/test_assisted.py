"""Channel routing, wa.me deep links, and the manual send queue (§6).

No network anywhere. Assisted channels produce share payloads and outbox rows that only
a human can mark sent — nothing here records a send on the organizer's behalf."""

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.channels import assisted_channels, email_channels, enqueue, route_channel, wa_link
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
#  Manual sends: the list, the walkthrough, and marking done by hand (§6/§7.3)
# --------------------------------------------------------------------------- #
def queue_invites(event):
    """Put every invitation for this event in the outbox, as the organizer would."""
    return enqueue(list(event.invitations.all()), "invite", "http://testserver")


def test_sharing_alone_does_not_mark_a_message_sent(staff_client, event):
    """The heart of the change: opening WhatsApp is not evidence anything was sent, so
    only an explicit "Sent it" moves the row (§7.3)."""
    dave = make_contact("Dave", (Kind.WHATSAPP, "+64211234567", True))
    inv = Invitation.objects.create(event=event, contact=dave)
    queue_invites(event)

    page = staff_client.get(reverse("event-queue", args=[event.pk])).content.decode()
    assert "1 of 1" in page and "Dave" in page and "wa.me/64211234567" in page
    assert inv.rsvp_path in page  # the link rides inside the message text

    # Loading the card, following the link — none of it records a send.
    delivery = inv.deliveries.get()
    assert delivery.status == Delivery.Status.PENDING
    inv.refresh_from_db()
    assert inv.state == State.QUEUED

    # Only the explicit mark does.
    staff_client.post(reverse("message-action", args=[delivery.pk]), {"action": "mark_sent"})
    delivery.refresh_from_db()
    inv.refresh_from_db()
    assert delivery.status == Delivery.Status.SENT
    assert delivery.sent_at is not None and delivery.sent_by.username == "sam"
    assert inv.state == State.SHARED  # assisted channel → shared, not sent


def test_walkthrough_advances_and_finishes(staff_client, event):
    dave = make_contact("Dave", (Kind.WHATSAPP, "+64211234567", True))
    ana = make_contact("Ana", (Kind.MESSENGER, "", True))
    inv_dave = Invitation.objects.create(event=event, contact=dave)
    inv_ana = Invitation.objects.create(event=event, contact=ana)
    queue_invites(event)

    page = staff_client.get(reverse("event-queue", args=[event.pk])).content.decode()
    assert "1 of 2" in page and "Dave" in page

    staff_client.post(
        reverse("message-action", args=[inv_dave.deliveries.get().pk]), {"action": "mark_sent"}
    )
    page = staff_client.get(reverse("event-queue", args=[event.pk])).content.decode()
    assert "1 of 1" in page and "Ana" in page and "Share" in page  # messenger card

    staff_client.post(
        reverse("message-action", args=[inv_ana.deliveries.get().pk]), {"action": "mark_sent"}
    )
    page = staff_client.get(reverse("event-queue", args=[event.pk])).content.decode()
    assert "All done" in page


def test_skip_sinks_a_card_and_persists(staff_client, event):
    """Deferral lives on the row, not the session, so the walk survives a new browser."""
    a = make_contact("Alice", (Kind.WHATSAPP, "+64211111111", True))
    b = make_contact("Bob", (Kind.WHATSAPP, "+64222222222", True))
    inv_a = Invitation.objects.create(event=event, contact=a)
    Invitation.objects.create(event=event, contact=b)
    queue_invites(event)

    staff_client.post(
        reverse("message-action", args=[inv_a.deliveries.get().pk]), {"action": "defer"}
    )
    assert inv_a.deliveries.get().status == Delivery.Status.PENDING  # skip sends nothing
    assert inv_a.deliveries.get().deferred_at is not None

    page = staff_client.get(reverse("event-queue", args=[event.pk])).content.decode()
    assert "Bob" in page and "1 of 2" in page  # Alice sank to the bottom, still counted

    # A brand-new client (no session) sees the same order — this used to be session state.
    fresh = Client()
    fresh.force_login(get_user_model().objects.get(username="sam"))
    page = fresh.get(reverse("event-queue", args=[event.pk])).content.decode()
    assert "Bob" in page


def test_messages_page_lists_manual_sends_with_actions(staff_client, event):
    dave = make_contact("Dave", (Kind.WHATSAPP, "+64211234567", True))
    Invitation.objects.create(event=event, contact=dave)
    queue_invites(event)

    page = staff_client.get(reverse("event-messages", args=[event.pk])).content.decode()
    assert "Manual sends — 1 to do" in page
    assert "Dave" in page and "wa.me/64211234567" in page
    assert "Mark sent" in page and "Work through them" in page


def test_dashboard_prompts_for_waiting_messages(staff_client, event):
    dave = make_contact("Dave", (Kind.WHATSAPP, "+64211234567", True))
    inv = Invitation.objects.create(event=event, contact=dave)
    dash_url = reverse("event-dashboard", args=[event.pk])

    assert "waiting to send" not in staff_client.get(dash_url).content.decode()
    queue_invites(event)

    dash = staff_client.get(dash_url).content.decode()
    assert "1 message waiting to send" in dash and "Open messages" in dash

    staff_client.post(
        reverse("message-action", args=[inv.deliveries.get().pk]), {"action": "mark_sent"}
    )
    assert "waiting to send" not in staff_client.get(dash_url).content.decode()


def test_household_two_whatsapp_parents_two_rows_same_link(staff_client, event):
    hh = Household.objects.create(name="The Hendersons")
    for name, phone in (("Jane", "+64211111111"), ("Mark", "+64222222222")):
        contact = make_contact(name, (Kind.WHATSAPP, phone, True))
        contact.household = hh
        contact.save()
    inv = Invitation.objects.create(event=event, household=hh)
    queue_invites(event)

    assert inv.deliveries.count() == 2  # one row per parent...
    assert {d.body for d in inv.deliveries.all()} == {inv.deliveries.first().body}  # ...same words
    assert all(inv.rsvp_path in d.body for d in inv.deliveries.all())  # ...same link

    page = staff_client.get(reverse("event-queue", args=[event.pk])).content.decode()
    assert "1 of 2" in page and "One of 2 in this household" in page

    jane_row = inv.deliveries.get(address_used="+64211111111")
    staff_client.post(reverse("message-action", args=[jane_row.pk]), {"action": "mark_sent"})

    page = staff_client.get(reverse("event-queue", args=[event.pk])).content.decode()
    assert "1 of 1" in page and "wa.me/64222222222" in page  # Mark still owed his


def test_mixed_household_splits_email_and_manual(staff_client, event):
    hh = Household.objects.create(name="Mixed")
    emailer = make_contact("Jane", (Kind.EMAIL, "jane@x.com", True))
    sharer = make_contact("Mark", (Kind.WHATSAPP, "+64222222222", True))
    for c in (emailer, sharer):
        c.household = hh
        c.save()
    inv = Invitation.objects.create(event=event, household=hh)
    assert [ch.value for ch in email_channels(inv)] == ["jane@x.com"]
    assert [ch.value for ch in assisted_channels(inv)] == ["+64222222222"]

    queue_invites(event)
    page = staff_client.get(reverse("event-messages", args=[event.pk])).content.decode()
    assert "Pending email — 1" in page and "Manual sends — 1 to do" in page


def test_nudge_queues_only_nonresponders(staff_client, event):
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

    staff_client.post(reverse("event-send", args=[event.pk]), {"action": "nudge"})
    nudges = Delivery.objects.filter(message_kind="nudge")
    assert [d.invitation_id for d in nudges] == [quiet.pk]


# --------------------------------------------------------------------------- #
#  Guardrails
# --------------------------------------------------------------------------- #
def test_queue_requires_staff(client, event):
    assert client.get(reverse("event-queue", args=[event.pk])).status_code == 302


def test_message_action_requires_staff(client, event):
    dave = make_contact("Dave", (Kind.WHATSAPP, "+64211234567", True))
    inv = Invitation.objects.create(event=event, contact=dave)
    queue_invites(event)
    resp = client.post(
        reverse("message-action", args=[inv.deliveries.get().pk]), {"action": "mark_sent"}
    )
    assert resp.status_code == 302  # login redirect
    assert inv.deliveries.get().status == Delivery.Status.PENDING


def test_message_action_rejects_unknown_action(staff_client, event):
    dave = make_contact("Dave", (Kind.WHATSAPP, "+64211234567", True))
    inv = Invitation.objects.create(event=event, contact=dave)
    queue_invites(event)
    resp = staff_client.post(
        reverse("message-action", args=[inv.deliveries.get().pk]), {"action": "hax"}
    )
    assert resp.status_code == 403


def test_message_action_on_missing_row_is_404(staff_client, event):
    assert (
        staff_client.post(reverse("message-action", args=[999999]), {"action": "defer"}).status_code
        == 404
    )


def test_a_guest_proposed_channel_is_never_queued(staff_client, event):
    """A guest-requested channel is untrusted until approved (§8). The outbox can't
    contain one because enqueue only ever routes to ACTIVE channels — the old code had
    to re-check this at share time."""
    dave = make_contact("Dave", (Kind.WHATSAPP, "+64211234567", True))
    inv = Invitation.objects.create(event=event, contact=dave)
    proposed = ContactChannel.objects.create(
        contact=dave,
        kind=Kind.WHATSAPP,
        value="+64299999999",
        status=ContactChannel.Status.PROPOSED,
        source=ContactChannel.Source.GUEST,
    )
    queue_invites(event)

    addresses = {d.address_used for d in inv.deliveries.all()}
    assert addresses == {"+64211234567"}
    assert proposed.value not in addresses


def test_retry_is_only_for_failed_rows(staff_client, event):
    dave = make_contact("Dave", (Kind.WHATSAPP, "+64211234567", True))
    inv = Invitation.objects.create(event=event, contact=dave)
    queue_invites(event)
    resp = staff_client.post(
        reverse("message-action", args=[inv.deliveries.get().pk]), {"action": "retry"}
    )
    assert resp.status_code == 403
