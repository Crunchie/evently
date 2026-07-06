"""Guest feedback from the RSVP page (§2.5): the durable record + best-effort email."""

from datetime import timedelta

import pytest
from django.utils import timezone

from core import channels, views
from core.models import Contact, Event, Feedback, Invitation


@pytest.fixture
def inv(db):
    event = Event.objects.create(
        title="Summer BBQ",
        starts_at=timezone.now() + timedelta(days=7),
        status=Event.Status.ACTIVE,
        host_display="Sam & Kate",
    )
    contact = Contact.objects.create(name="Alex Doe")
    return Invitation.objects.create(event=event, contact=contact)


@pytest.fixture
def no_email(monkeypatch):
    """Neuter the outbound email — the record is what these tests assert on."""
    sent = []
    monkeypatch.setattr(views, "send_feedback_email", lambda fb: sent.append(fb) or True)
    return sent


# ---------------------------------------------------------------- the record
def test_feedback_saves_record_and_emails(client, inv, no_email):
    resp = client.post(
        f"{inv.rsvp_path}/feedback",
        {"message": "The map doesn't load on my phone", "reply_email": "alex@example.com"},
        HTTP_USER_AGENT="TestBrowser/1.0",
        HTTP_REFERER=f"http://testserver{inv.rsvp_path}",
    )
    assert resp.status_code == 302
    assert resp.url == f"{inv.rsvp_path}?feedback=1"

    fb = Feedback.objects.get()
    assert fb.message == "The map doesn't load on my phone"
    assert fb.reply_email == "alex@example.com"
    assert fb.invitation == inv
    assert fb.event == inv.event
    assert fb.user_agent == "TestBrowser/1.0"
    assert fb.page_path == f"http://testserver{inv.rsvp_path}"
    assert no_email == [fb]  # best-effort notification fired with the saved row


def test_blank_message_is_rejected_no_record(client, inv, no_email):
    resp = client.post(f"{inv.rsvp_path}/feedback", {"message": "   "})
    assert resp.url == f"{inv.rsvp_path}?feedback=error"
    assert Feedback.objects.count() == 0
    assert no_email == []


def test_invalid_reply_email_dropped_but_saved(client, inv, no_email):
    client.post(
        f"{inv.rsvp_path}/feedback",
        {"message": "typo in the address", "reply_email": "not-an-email"},
    )
    fb = Feedback.objects.get()
    assert fb.reply_email == ""  # silently dropped; the report still stands


def test_message_truncated_to_limit(client, inv, no_email):
    client.post(f"{inv.rsvp_path}/feedback", {"message": "x" * 5000})
    assert len(Feedback.objects.get().message) == views.MAX_FEEDBACK_LEN


def test_revoked_invite_cannot_leave_feedback(client, inv, no_email):
    inv.advance_state(Invitation.State.REVOKED)
    resp = client.post(f"{inv.rsvp_path}/feedback", {"message": "hi"})
    assert resp.status_code == 410
    assert Feedback.objects.count() == 0


def test_feedback_is_post_only(client, inv):
    assert client.get(f"{inv.rsvp_path}/feedback").status_code == 405


def test_unknown_token_404(client, db):
    assert client.post("/i/nope/feedback", {"message": "hi"}).status_code == 404


# ---------------------------------------------------------------- the UI + banner
def test_rsvp_page_shows_feedback_modal_and_trigger(client, inv):
    content = client.get(inv.rsvp_path).content.decode()
    assert "data-feedback-open" in content  # subtle footer trigger
    assert 'id="feedback-modal"' in content  # the modal it opens
    assert 'action="' + inv.rsvp_path + '/feedback"' in content
    assert 'name="message"' in content


def test_feedback_modal_auto_opens_on_error(client, inv):
    # A rejected submit reloads with ?feedback=error → the modal reopens for a retry.
    content = client.get(f"{inv.rsvp_path}?feedback=error").content.decode()
    assert "data-open-on-load" in content


def test_no_auto_open_on_normal_load(client, inv):
    assert "data-open-on-load" not in client.get(inv.rsvp_path).content.decode()


def test_success_banner_after_send(client, inv):
    content = client.get(f"{inv.rsvp_path}?feedback=1").content.decode()
    assert "Thanks for the feedback" in content


# ---------------------------------------------------------------- the email helper
def test_send_feedback_email_noop_without_config(settings, inv):
    settings.RESEND_API_KEY = ""
    fb = Feedback.objects.create(invitation=inv, event=inv.event, message="hi")
    assert channels.send_feedback_email(fb) is False


def test_send_feedback_email_dispatches(settings, monkeypatch, inv):
    settings.RESEND_API_KEY = "re_test"
    settings.EMAIL_FROM = "Sam <invites@evently.test>"
    settings.FEEDBACK_EMAIL = "sam@personal.test"
    settings.EMAIL_REPLY_TO = "replies@evently.test"
    captured = {}
    monkeypatch.setattr(channels.resend.Emails, "send", lambda payload: captured.update(payload))

    fb = Feedback.objects.create(
        invitation=inv, event=inv.event, message="broken map", reply_email="alex@example.com"
    )
    assert channels.send_feedback_email(fb) is True
    assert captured["to"] == ["sam@personal.test"]
    assert captured["reply_to"] == "alex@example.com"  # replies reach the guest
    assert "broken map" in captured["text"]
    assert "Summer BBQ" in captured["text"]


def test_send_feedback_email_swallows_provider_error(settings, monkeypatch, inv):
    settings.RESEND_API_KEY = "re_test"
    settings.EMAIL_FROM = "Sam <invites@evently.test>"
    settings.FEEDBACK_EMAIL = "sam@personal.test"

    def _boom(payload):
        raise RuntimeError("resend down")

    monkeypatch.setattr(channels.resend.Emails, "send", _boom)
    fb = Feedback.objects.create(invitation=inv, event=inv.event, message="hi")
    assert channels.send_feedback_email(fb) is False  # record already saved; no raise
