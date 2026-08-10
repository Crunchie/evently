"""The Messages screen itself (§8.1) and per-row editing (§7.4).

Covers the thing the outbox was built for: one page that answers "what has and hasn't
gone out for this event", and the ability to read — and rewrite — a message before it does.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core import channels
from core.channels import enqueue
from core.models import Contact, ContactChannel, Delivery, Event, Household, Invitation

Kind = ContactChannel.Kind
Status = Delivery.Status


@pytest.fixture
def staff_client(client, django_user_model):
    user = django_user_model.objects.create_superuser("sam", "sam@example.com", "pw-strong-123")
    client.force_login(user)
    return client


@pytest.fixture
def fake_send(monkeypatch):
    batches = []

    def _fake(messages):
        batches.append(messages)
        return [f"re_{i}" for i in range(len(messages))]

    monkeypatch.setattr(channels, "send_email_batch", _fake)
    return batches


@pytest.fixture
def event(db):
    return Event.objects.create(
        title="Sam's 40th",
        starts_at=timezone.now() + timedelta(days=7),
        status=Event.Status.ACTIVE,
        host_display="Sam & Kate",
        location_text="42 Maple Avenue",
    )


def with_channel(name, kind, value="", household=None):
    contact = Contact.objects.create(name=name, household=household)
    ContactChannel.objects.create(contact=contact, kind=kind, value=value, is_preferred=True)
    return contact


@pytest.fixture
def mixed_event(event):
    """One event carrying every row type the screen has to display."""
    hh = Household.objects.create(name="The Hendersons")
    with_channel("Jane", Kind.WHATSAPP, "+64211111111", household=hh)
    with_channel("Mark", Kind.WHATSAPP, "+64222222222", household=hh)
    Invitation.objects.create(event=event, household=hh)
    Invitation.objects.create(event=event, contact=with_channel("Kate", Kind.EMAIL, "kate@x.com"))
    Invitation.objects.create(event=event, contact=with_channel("Jo", Kind.MESSENGER))
    Invitation.objects.create(event=event, contact=Contact.objects.create(name="Auntie Ngaire"))
    enqueue(list(event.invitations.all()), "invite", "http://testserver")
    return event


# --------------------------------------------------------------------------- #
#  The screen
# --------------------------------------------------------------------------- #
def test_messages_screen_sections(staff_client, mixed_event):
    page = staff_client.get(reverse("event-messages", args=[mixed_event.pk])).content.decode()

    assert "Pending email — 1" in page  # Kate
    assert "Manual sends — 3 to do" in page  # two Hendersons + Jo
    assert "Can't send — 1" in page and "Auntie Ngaire" in page
    assert "Send all 1" in page
    assert "All messages" in page
    # The message body is on the page, not just a count — that was the whole complaint.
    assert "42 Maple Avenue" in page


def test_dashboard_counts_everything_waiting(staff_client, mixed_event):
    dash = staff_client.get(reverse("event-dashboard", args=[mixed_event.pk])).content.decode()
    assert "5 messages waiting to send" in dash
    assert "1 email" in dash and "3 to send by hand" in dash and "1 with no channel" in dash


def test_sent_rows_move_to_history(staff_client, mixed_event, fake_send):
    kate = Invitation.objects.get(contact__name="Kate")
    staff_client.post(reverse("event-send-pending", args=[mixed_event.pk]))
    page = staff_client.get(reverse("event-messages", args=[mixed_event.pk])).content.decode()

    assert "Pending email" not in page  # section gone
    assert "Manual sends — 3 to do" in page  # untouched
    assert "Kate" in page  # ...but Kate is still on the page, under history
    assert "<strong>1</strong> sent" in page  # the summary strip counted it
    # The history row is the sent one, timestamped.
    history = page.split("All messages")[1]
    assert "kate@x.com" in history
    assert kate.deliveries.get().sent_at is not None


def test_history_filters(staff_client, mixed_event, fake_send):
    staff_client.post(reverse("event-send-pending", args=[mixed_event.pk]))
    url = reverse("event-messages", args=[mixed_event.pk])

    page = staff_client.get(url, {"status": "sent"}).content.decode()
    assert "Kate" in page
    page = staff_client.get(url, {"status": "cancelled"}).content.decode()
    assert "Nothing sent yet." in page  # filtered to empty, not crashed
    page = staff_client.get(url, {"kind": "nudge"}).content.decode()
    assert "Nothing sent yet." in page


def test_blocked_row_can_be_marked_done_by_hand(staff_client, mixed_event):
    """ "I told them at the school gate" — the row closes with no channel involved."""
    ngaire = Invitation.objects.get(contact__name="Auntie Ngaire")
    row = ngaire.deliveries.get()
    assert row.status == Status.BLOCKED

    staff_client.post(reverse("message-action", args=[row.pk]), {"action": "mark_sent"})
    row.refresh_from_db()
    assert row.status == Status.SENT and row.sent_by.username == "sam"

    page = staff_client.get(reverse("event-messages", args=[mixed_event.pk])).content.decode()
    assert "Can't send" not in page


def test_walkthrough_and_list_act_on_the_same_rows(staff_client, mixed_event):
    """The card is a view of the outbox, not parallel state — they can't disagree."""
    jo = Invitation.objects.get(contact__name="Jo")
    staff_client.post(
        reverse("message-action", args=[jo.deliveries.get().pk]), {"action": "mark_sent"}
    )

    walk = staff_client.get(reverse("event-queue", args=[mixed_event.pk])).content.decode()
    page = staff_client.get(reverse("event-messages", args=[mixed_event.pk])).content.decode()
    assert "1 of 2" in walk
    assert "Manual sends — 2 to do" in page


def test_message_action_lands_back_where_it_was_pressed(staff_client, mixed_event):
    jo = Invitation.objects.get(contact__name="Jo")
    walk_url = reverse("event-queue", args=[mixed_event.pk])
    resp = staff_client.post(
        reverse("message-action", args=[jo.deliveries.get().pk]),
        {"action": "defer", "next": f"{walk_url}?n=0"},
    )
    assert resp.status_code == 302 and walk_url in resp["Location"]


# --------------------------------------------------------------------------- #
#  Editing one message (§7.4)
# --------------------------------------------------------------------------- #
@pytest.fixture
def email_row(event):
    inv = Invitation.objects.create(
        event=event, contact=with_channel("Kate", Kind.EMAIL, "kate@x.com")
    )
    enqueue([inv], "invite", "http://testserver")
    return inv.deliveries.get()


def test_edit_rejects_a_body_without_the_link(staff_client, email_row):
    resp = staff_client.post(
        reverse("message-edit", args=[email_row.pk]),
        {"subject": "Hi", "body": "come to my party"},
    )
    assert resp.status_code == 200
    assert "Keep their personal link" in resp.content.decode()
    email_row.refresh_from_db()
    assert not email_row.is_edited and "come to my party" not in email_row.body


def test_edit_saves_and_is_what_gets_sent(staff_client, email_row, fake_send):
    url = f"http://testserver{email_row.invitation.rsvp_path}"
    body = f"Kate — it's black tie, don't let me down.\n\n{url}"
    resp = staff_client.post(
        reverse("message-edit", args=[email_row.pk]), {"subject": "Custom", "body": body}
    )
    assert resp.status_code == 302

    email_row.refresh_from_db()
    assert email_row.is_edited and email_row.subject == "Custom"

    staff_client.post(reverse("event-send-pending", args=[email_row.invitation.event_id]))
    message = fake_send[0][0]
    assert message["subject"] == "Custom"
    assert "black tie" in message["text"] and "black tie" in message["html"]
    # The link still becomes the button, and isn't left lying in the text.
    assert f'href="{url}"' in message["html"]
    assert f">{url}<" not in message["html"]


def test_an_edited_message_is_not_overwritten_by_an_event_change(
    staff_client, email_row, fake_send
):
    url = f"http://testserver{email_row.invitation.rsvp_path}"
    staff_client.post(
        reverse("message-edit", args=[email_row.pk]),
        {"subject": "Custom", "body": f"My own words.\n\n{url}"},
    )
    event = email_row.invitation.event
    event.location_text = "Somewhere else"
    event.save()

    staff_client.post(reverse("event-send-pending", args=[event.pk]))
    assert "My own words." in fake_send[0][0]["text"]
    assert "Somewhere else" not in fake_send[0][0]["text"]


def test_revert_restores_the_template_and_auto_refresh(staff_client, email_row, fake_send):
    url = f"http://testserver{email_row.invitation.rsvp_path}"
    staff_client.post(
        reverse("message-edit", args=[email_row.pk]),
        {"subject": "Custom", "body": f"My own words.\n\n{url}"},
    )
    staff_client.post(reverse("message-action", args=[email_row.pk]), {"action": "revert"})

    email_row.refresh_from_db()
    assert not email_row.is_edited
    assert "You're invited" in email_row.subject and "My own words" not in email_row.body

    # ...and it tracks the event again.
    event = email_row.invitation.event
    event.location_text = "The New Hall"
    event.save()
    staff_client.post(reverse("event-send-pending", args=[event.pk]))
    assert "The New Hall" in fake_send[0][0]["text"]


def test_a_sent_message_cannot_be_edited(staff_client, email_row, fake_send):
    staff_client.post(reverse("event-send-pending", args=[email_row.invitation.event_id]))
    assert staff_client.get(reverse("message-edit", args=[email_row.pk])).status_code == 403


def test_regenerating_a_token_refreshes_queued_links(staff_client, email_row):
    """A dead link in a queued message would be silently useless to the guest."""
    invitation = email_row.invitation
    old_path = invitation.rsvp_path
    staff_client.post(reverse("invitation-action", args=[invitation.pk]), {"action": "regenerate"})

    invitation.refresh_from_db()
    email_row.refresh_from_db()
    assert invitation.rsvp_path != old_path
    assert old_path not in email_row.body
    assert invitation.rsvp_path in email_row.body


def test_edit_requires_staff(client, email_row):
    assert client.get(reverse("message-edit", args=[email_row.pk])).status_code == 302
