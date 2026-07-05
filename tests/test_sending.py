"""Phase 4: synchronous email dispatch via the send/notify actions (§2.3/§2.4/§9).
The Resend call is patched out — outcomes and state transitions are what's under test."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core import channels
from core.models import Contact, ContactChannel, Delivery, Event, Household, Invitation

State = Invitation.State


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


def send_url(event):
    return reverse("event-send", args=[event.pk])


def test_send_invites_flow(staff_client, event, fake_send, settings):
    settings.EMAIL_REPLY_TO = "hosts@example.com"  # drives the List-Unsubscribe mailto
    inv = Invitation.objects.create(event=event, contact=contact_with_email("Alex Doe", "a@x.com"))
    no_email = Invitation.objects.create(event=event, contact=Contact.objects.create(name="Tom"))

    # review screen shows the breakdown
    content = staff_client.get(send_url(event)).content.decode()
    assert "Send 1 email invite" in content and "Tom" in content

    resp = staff_client.post(send_url(event), {"action": "invites"})
    assert resp.status_code == 302
    assert "did=invites" in resp["Location"] and "sent=1" in resp["Location"]

    inv.refresh_from_db()
    event.refresh_from_db()
    no_email.refresh_from_db()
    assert inv.state == State.SENT
    assert event.status == Event.Status.ACTIVE  # first send flips draft → active
    assert no_email.state == State.PENDING  # skipped, not silently "sent"

    delivery = inv.deliveries.get()
    assert delivery.status == Delivery.Status.SENT
    assert delivery.provider_message_id == "re_0"
    assert delivery.address_used == "a@x.com"

    message = fake_send[0][0]
    assert message["to"] == ["a@x.com"]
    assert "You're invited" in message["subject"] and "Summer BBQ" in message["subject"]
    assert inv.rsvp_path in message["text"]
    # List-Unsubscribe present for deliverability (mailto → reply-to inbox).
    assert (
        message["headers"]["List-Unsubscribe"] == "<mailto:hosts@example.com?subject=unsubscribe>"
    )

    # idempotent: nothing pending anymore → second send is a no-op
    resp = staff_client.post(send_url(event), {"action": "invites"})
    assert "sent=0" in resp["Location"]
    assert inv.deliveries.count() == 1


def test_invite_message_carries_event_details(event):
    from core.messaging import build_message

    event.location_text = "42 Maple Avenue"
    event.description = "Bring a plate to share!"
    event.save()
    inv = Invitation.objects.create(event=event, contact=contact_with_email("Alex", "a@x.com"))

    msg = build_message("invite", inv, "https://x.test/i/abc")
    # when, where and the description all appear so the email stands on its own
    assert "Summer BBQ" in msg["text"]
    assert "42 Maple Avenue" in msg["text"] and "42 Maple Avenue" in msg["html"]
    assert "Bring a plate to share!" in msg["text"] and "Bring a plate to share!" in msg["html"]


def test_provider_error_marks_failed_and_keeps_state(staff_client, event, monkeypatch):
    def _boom(messages):
        raise RuntimeError("resend down")

    monkeypatch.setattr(channels, "send_email_batch", _boom)
    inv = Invitation.objects.create(event=event, contact=contact_with_email("Alex", "a@x.com"))

    resp = staff_client.post(send_url(event), {"action": "invites"})
    assert "failed=1" in resp["Location"]

    inv.refresh_from_db()
    delivery = inv.deliveries.get()
    assert delivery.status == Delivery.Status.FAILED and "resend down" in delivery.error
    assert inv.state == State.PENDING  # nothing went out
    # ...and it now shows up as retryable
    assert "Retry" in staff_client.get(send_url(event)).content.decode()


def test_short_provider_response_fails_the_unmatched_tail(staff_client, event, monkeypatch):
    """If Resend returns fewer ids than messages, the tail must be FAILED, not
    left QUEUED forever and missing from the ✓/✗ counts."""
    monkeypatch.setattr(channels, "send_email_batch", lambda messages: ["re_0"])  # one id short
    for n, email in enumerate(("a@x.com", "b@x.com")):
        Invitation.objects.create(event=event, contact=contact_with_email(f"Guest {n}", email))

    resp = staff_client.post(send_url(event), {"action": "invites"})
    assert "sent=1" in resp["Location"] and "failed=1" in resp["Location"]
    assert Delivery.objects.filter(status=Delivery.Status.FAILED).count() == 1
    assert not Delivery.objects.filter(status=Delivery.Status.QUEUED).exists()


def test_household_sends_same_link_to_each_parent(staff_client, event, fake_send):
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
    addresses = {d.address_used for d in inv.deliveries.all()}
    assert addresses == {"jane@x.com", "mark@x.com"}
    # both copies carry the same envelope link
    assert all(inv.rsvp_path in m["text"] for m in fake_send[0])


def test_nudge_targets_only_nonresponders(staff_client, event, fake_send):
    quiet = Invitation.objects.create(event=event, contact=contact_with_email("Quiet", "q@x.com"))
    replied = Invitation.objects.create(event=event, contact=contact_with_email("Fast", "f@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})

    # one guest responds through their link
    attendee = replied.attendees.get()
    staff_client.post(replied.rsvp_path, {f"status_{attendee.pk}": "going"})

    fake_send.clear()
    resp = staff_client.post(send_url(event), {"action": "nudge"})
    assert "sent=1" in resp["Location"]
    assert fake_send[0][0]["to"] == ["q@x.com"]
    assert "nudge" in fake_send[0][0]["subject"].lower() or "hoping" in fake_send[0][0]["subject"]
    quiet.refresh_from_db()
    assert quiet.state == State.SENT  # nudge doesn't fake progress


def test_cancel_action_cancels_and_notifies(staff_client, event, fake_send):
    inv = Invitation.objects.create(event=event, contact=contact_with_email("Alex", "a@x.com"))
    staff_client.post(send_url(event), {"action": "invites"})

    fake_send.clear()
    resp = staff_client.post(send_url(event), {"action": "cancel"})
    assert "did=cancel" in resp["Location"] and "sent=1" in resp["Location"]

    event.refresh_from_db()
    assert event.status == Event.Status.CANCELLED
    assert "Cancelled" in fake_send[0][0]["subject"]

    # guest link now shows the cancelled state and refuses RSVPs
    attendee = inv.attendees.get()
    assert staff_client.post(inv.rsvp_path, {f"status_{attendee.pk}": "going"}).status_code == 403


def test_send_page_requires_staff(client, event):
    assert client.get(send_url(event)).status_code == 302  # to admin login
