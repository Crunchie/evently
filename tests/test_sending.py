"""The outbox: queueing messages, then sending the email ones (§2.3/§2.4/§9).

The Resend call is patched out — what's under test is that **queueing and sending are
separate**, that a queued row says what it will say before it goes, and that state moves
only when something actually left.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core import channels
from core.models import Contact, ContactChannel, Delivery, Event, Household, Invitation

State = Invitation.State
Status = Delivery.Status


@pytest.fixture
def staff_client(client, django_user_model):
    user = django_user_model.objects.create_superuser("sam", "sam@example.com", "pw-strong-123")
    client.force_login(user)
    return client


@pytest.fixture
def fake_send(monkeypatch):
    """Capture outgoing batches; return predictable provider ids."""
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
        status=Event.Status.DRAFT,
        host_display="Sam & Kate",
    )


def contact_with_email(name, email):
    contact = Contact.objects.create(name=name)
    ContactChannel.objects.create(
        contact=contact, kind=ContactChannel.Kind.EMAIL, value=email, is_preferred=True
    )
    return contact


def _whatsapp_contact(name, phone):
    contact = Contact.objects.create(name=name)
    ContactChannel.objects.create(
        contact=contact, kind=ContactChannel.Kind.WHATSAPP, value=phone, is_preferred=True
    )
    return contact


def send_url(event):
    return reverse("event-send", args=[event.pk])


def messages_url(event):
    return reverse("event-messages", args=[event.pk])


def send_pending_url(event):
    return reverse("event-send-pending", args=[event.pk])


# --------------------------------------------------------------------------- #
#  Queueing sends nothing
# --------------------------------------------------------------------------- #
def test_queueing_invites_sends_nothing(staff_client, event, fake_send):
    inv = Invitation.objects.create(event=event, contact=contact_with_email("Alex", "a@x.com"))

    resp = staff_client.post(send_url(event), {"action": "invites"})
    assert resp.status_code == 302
    assert "/messages/" in resp["Location"] and "queued=1" in resp["Location"]

    assert not fake_send  # the whole point: nothing left the building
    inv.refresh_from_db()
    assert inv.state == State.QUEUED
    delivery = inv.deliveries.get()
    assert delivery.status == Status.PENDING
    assert delivery.message_kind == "invite"
    assert delivery.address_used == "a@x.com"
    assert delivery.sent_at is None


def test_queued_row_carries_the_message_it_will_send(staff_client, event):
    event.location_text = "42 Maple Avenue"
    event.description = "Bring a plate to share!"
    event.save()
    inv = Invitation.objects.create(event=event, contact=contact_with_email("Alex", "a@x.com"))

    staff_client.post(send_url(event), {"action": "invites"})
    delivery = inv.deliveries.get()
    assert "You're invited" in delivery.subject and "Summer BBQ" in delivery.subject
    assert "42 Maple Avenue" in delivery.body
    assert "Bring a plate to share!" in delivery.body
    assert inv.rsvp_path in delivery.body  # the link is the point of the message

    # ...and the screen shows it, so "what is about to go out" is answerable.
    page = staff_client.get(messages_url(event)).content.decode()
    assert "Pending email — 1" in page and "42 Maple Avenue" in page


def test_queueing_twice_does_not_double_queue(staff_client, event):
    inv = Invitation.objects.create(event=event, contact=contact_with_email("Alex", "a@x.com"))

    staff_client.post(send_url(event), {"action": "invites"})
    resp = staff_client.post(send_url(event), {"action": "invites"})

    assert inv.deliveries.count() == 1
    assert "already_queued=1" in resp["Location"] or "queued=0" in resp["Location"]


def test_guest_with_no_channel_gets_a_blocked_row(staff_client, event):
    """Not silently skipped: the outbox is meant to be the complete list."""
    inv = Invitation.objects.create(event=event, contact=Contact.objects.create(name="Tom"))

    staff_client.post(send_url(event), {"action": "invites"})
    delivery = inv.deliveries.get()
    assert delivery.status == Status.BLOCKED
    assert delivery.channel_id is None

    page = staff_client.get(messages_url(event)).content.decode()
    assert "Can't send — 1" in page and "Tom" in page


def test_queueing_flips_a_draft_active(staff_client, event):
    """can_rsvp needs ACTIVE — a draft whose invites went out would hand every guest a
    link they can't answer."""
    Invitation.objects.create(event=event, contact=contact_with_email("Alex", "a@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})
    event.refresh_from_db()
    assert event.status == Event.Status.ACTIVE


# --------------------------------------------------------------------------- #
#  Sending the pending email
# --------------------------------------------------------------------------- #
def test_send_pending_emails(staff_client, event, fake_send, settings):
    settings.EMAIL_REPLY_TO = "hosts@example.com"  # drives the List-Unsubscribe mailto
    inv = Invitation.objects.create(event=event, contact=contact_with_email("Alex", "a@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})

    resp = staff_client.post(send_pending_url(event))
    assert "did=sent" in resp["Location"] and "sent=1" in resp["Location"]

    inv.refresh_from_db()
    assert inv.state == State.SENT
    delivery = inv.deliveries.get()
    assert delivery.status == Status.SENT
    assert delivery.provider_message_id == "re_0"
    assert delivery.sent_at is not None
    assert delivery.sent_by.username == "sam"

    message = fake_send[0][0]
    assert message["to"] == ["a@x.com"]
    assert inv.rsvp_path in message["text"]
    assert (
        message["headers"]["List-Unsubscribe"] == "<mailto:hosts@example.com?subject=unsubscribe>"
    )

    # Sent rows leave the pending section and appear in the history.
    page = staff_client.get(messages_url(event)).content.decode()
    assert "Pending email" not in page
    assert "All messages" in page and "Alex" in page

    # Nothing pending → second press is a no-op.
    resp = staff_client.post(send_pending_url(event))
    assert "sent=0" in resp["Location"]
    assert inv.deliveries.count() == 1


def test_send_pending_sends_every_kind_at_once(staff_client, event, fake_send):
    """No kind filter on the button: it sends what the list shows (§7.2)."""
    a = Invitation.objects.create(event=event, contact=contact_with_email("A", "a@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})
    staff_client.post(send_pending_url(event))
    fake_send.clear()

    # Now queue a nudge for A *and* an invite for a newcomer, then send once.
    Invitation.objects.create(event=event, contact=contact_with_email("B", "b@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})
    staff_client.post(send_url(event), {"action": "nudge"})

    resp = staff_client.post(send_pending_url(event))
    assert "sent=2" in resp["Location"]
    kinds = {d.message_kind for d in Delivery.objects.filter(status=Status.SENT)}
    assert kinds == {"invite", "nudge"}
    assert a.deliveries.filter(message_kind="nudge", status=Status.SENT).exists()


def test_cancelling_a_row_holds_it_back(staff_client, event, fake_send):
    """ "Don't send" is how you hold one message back — there are no tick-boxes."""
    keep = Invitation.objects.create(event=event, contact=contact_with_email("Keep", "k@x.com"))
    drop = Invitation.objects.create(event=event, contact=contact_with_email("Drop", "d@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})

    staff_client.post(
        reverse("message-action", args=[drop.deliveries.get().pk]), {"action": "cancel"}
    )
    resp = staff_client.post(send_pending_url(event))

    assert "sent=1" in resp["Location"]
    assert keep.deliveries.get().status == Status.SENT
    assert drop.deliveries.get().status == Status.CANCELLED
    assert [m["to"] for m in fake_send[0]] == [["k@x.com"]]


def test_provider_error_marks_failed_and_keeps_state(staff_client, event, monkeypatch):
    monkeypatch.setattr(
        channels, "send_email_batch", lambda messages: (_ for _ in ()).throw(RuntimeError("down"))
    )
    inv = Invitation.objects.create(event=event, contact=contact_with_email("Alex", "a@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})

    resp = staff_client.post(send_pending_url(event))
    assert "failed=1" in resp["Location"]

    inv.refresh_from_db()
    delivery = inv.deliveries.get()
    assert delivery.status == Status.FAILED and "down" in delivery.error
    assert inv.state == State.QUEUED  # queued, never sent

    # It surfaces as needing attention, with a retry.
    page = staff_client.get(messages_url(event)).content.decode()
    assert "Needs attention" in page and "Retry" in page


def test_retry_puts_a_failed_row_back_in_the_queue(staff_client, event, fake_send, monkeypatch):
    monkeypatch.setattr(
        channels, "send_email_batch", lambda messages: (_ for _ in ()).throw(RuntimeError("down"))
    )
    inv = Invitation.objects.create(event=event, contact=contact_with_email("Alex", "a@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})
    staff_client.post(send_pending_url(event))

    delivery = inv.deliveries.get()
    staff_client.post(reverse("message-action", args=[delivery.pk]), {"action": "retry"})
    delivery.refresh_from_db()
    assert delivery.status == Status.PENDING and delivery.error == ""

    # A retry re-queues rather than re-sending, so the address can be fixed in between.
    monkeypatch.setattr(channels, "send_email_batch", lambda messages: ["re_0"])
    staff_client.post(send_pending_url(event))
    delivery.refresh_from_db()
    assert delivery.status == Status.SENT


def test_short_provider_response_fails_the_unmatched_tail(staff_client, event, monkeypatch):
    """If Resend returns fewer ids than messages, the tail must be FAILED, not left
    pending forever and missing from the ✓/✗ counts."""
    monkeypatch.setattr(channels, "send_email_batch", lambda messages: ["re_0"])  # one id short
    for n, email in enumerate(("a@x.com", "b@x.com")):
        Invitation.objects.create(event=event, contact=contact_with_email(f"Guest {n}", email))
    staff_client.post(send_url(event), {"action": "invites"})

    resp = staff_client.post(send_pending_url(event))
    assert "sent=1" in resp["Location"] and "failed=1" in resp["Location"]
    assert Delivery.objects.filter(status=Status.FAILED).count() == 1
    assert not Delivery.objects.filter(status=Status.PENDING).exists()


def test_batches_over_the_provider_limit(staff_client, event, fake_send, monkeypatch):
    monkeypatch.setattr(channels, "RESEND_BATCH_LIMIT", 2)
    for n in range(5):
        Invitation.objects.create(event=event, contact=contact_with_email(f"G{n}", f"g{n}@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})

    resp = staff_client.post(send_pending_url(event))
    assert "sent=5" in resp["Location"]
    assert [len(b) for b in fake_send] == [2, 2, 1]


def test_household_queues_same_link_to_each_parent(staff_client, event, fake_send):
    hh = Household.objects.create(name="The Hendersons")
    jane = contact_with_email("Jane", "jane@x.com")
    mark = contact_with_email("Mark", "mark@x.com")
    dup = contact_with_email("Ollie", "jane@x.com")  # shares Jane's address
    for c in (jane, mark, dup):
        c.household = hh
        c.save()
    inv = Invitation.objects.create(event=event, household=hh)

    staff_client.post(send_url(event), {"action": "invites"})

    assert inv.deliveries.count() == 2  # deduped by address
    assert {d.address_used for d in inv.deliveries.all()} == {"jane@x.com", "mark@x.com"}
    assert all(inv.rsvp_path in d.body for d in inv.deliveries.all())


def test_nudge_targets_only_nonresponders(staff_client, event, fake_send):
    quiet = Invitation.objects.create(event=event, contact=contact_with_email("Quiet", "q@x.com"))
    replied = Invitation.objects.create(event=event, contact=contact_with_email("Fast", "f@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})
    staff_client.post(send_pending_url(event))

    attendee = replied.attendees.get()
    staff_client.post(replied.rsvp_path, {f"status_{attendee.pk}": "going"})

    staff_client.post(send_url(event), {"action": "nudge"})
    nudges = Delivery.objects.filter(message_kind="nudge")
    assert [d.invitation_id for d in nudges] == [quiet.pk]
    quiet.refresh_from_db()
    assert quiet.state == State.SENT  # queueing a nudge doesn't fake progress


def test_reminder_targets_going_and_maybe(staff_client, event, fake_send):
    going = Invitation.objects.create(event=event, contact=contact_with_email("Go", "g@x.com"))
    quiet = Invitation.objects.create(event=event, contact=contact_with_email("Quiet", "q@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})
    staff_client.post(send_pending_url(event))
    staff_client.post(going.rsvp_path, {f"status_{going.attendees.get().pk}": "going"})

    staff_client.post(send_url(event), {"action": "reminder"})
    reminders = Delivery.objects.filter(message_kind="reminder")
    assert [d.invitation_id for d in reminders] == [going.pk]
    assert not quiet.deliveries.filter(message_kind="reminder").exists()


# --------------------------------------------------------------------------- #
#  Cancellation
# --------------------------------------------------------------------------- #
def test_cancel_drops_queued_messages_before_queueing_the_notice(staff_client, event, fake_send):
    """The worst failure this app could have: an invite going out after a cancellation."""
    inv = Invitation.objects.create(event=event, contact=contact_with_email("Alex", "a@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})
    staff_client.post(send_pending_url(event))
    # A second invite queued but not yet sent — this is the dangerous one.
    late = Invitation.objects.create(event=event, contact=contact_with_email("Late", "l@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})
    assert late.deliveries.get().status == Status.PENDING

    staff_client.post(send_url(event), {"action": "cancel"})

    event.refresh_from_db()
    assert event.status == Event.Status.CANCELLED
    assert late.deliveries.get(message_kind="invite").status == Status.CANCELLED
    assert inv.deliveries.filter(message_kind="cancellation", status=Status.PENDING).exists()

    fake_send.clear()
    staff_client.post(send_pending_url(event))
    assert all("Cancelled" in m["subject"] for m in fake_send[0])

    # Guest link now shows the cancelled state and refuses RSVPs.
    attendee = inv.attendees.get()
    assert staff_client.post(inv.rsvp_path, {f"status_{attendee.pk}": "going"}).status_code == 403


def test_uninvite_drops_queued_messages(staff_client, event):
    inv = Invitation.objects.create(event=event, contact=contact_with_email("Alex", "a@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})

    staff_client.post(reverse("invitation-action", args=[inv.pk]), {"action": "revoke"})
    assert inv.deliveries.get().status == Status.CANCELLED


# --------------------------------------------------------------------------- #
#  Message content
# --------------------------------------------------------------------------- #
def test_invite_message_carries_event_details(event):
    from core.messaging import build_message

    event.location_text = "42 Maple Avenue"
    event.description = "Bring a plate to share!"
    event.save()
    inv = Invitation.objects.create(event=event, contact=contact_with_email("Alex", "a@x.com"))

    msg = build_message("invite", inv, "https://x.test/i/abc")
    assert "Summer BBQ" in msg["text"]
    assert "42 Maple Avenue" in msg["text"] and "42 Maple Avenue" in msg["html"]
    assert "Bring a plate to share!" in msg["text"] and "Bring a plate to share!" in msg["html"]
    # The raw link never appears as text beside the button — the button carries it.
    assert 'href="https://x.test/i/abc"' in msg["html"]
    assert ">https://x.test/i/abc<" not in msg["html"]


def test_unedited_row_picks_up_an_event_change_at_send(staff_client, event, fake_send):
    """Queued, then the venue changes: the message that goes out is the current one, with
    no second message queued (§12.3)."""
    Invitation.objects.create(event=event, contact=contact_with_email("Alex", "a@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})

    event.location_text = "The New Hall"
    event.save()
    staff_client.post(send_pending_url(event))

    assert "The New Hall" in fake_send[0][0]["text"]
    assert Delivery.objects.count() == 1  # no extra "update" appeared


def test_send_page_requires_staff(client, event):
    assert client.get(send_url(event)).status_code == 302  # to admin login


def test_messages_page_requires_staff(client, event):
    assert client.get(messages_url(event)).status_code == 302
