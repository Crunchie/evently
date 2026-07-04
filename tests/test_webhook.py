"""Resend bounce webhook (§9): Svix-scheme signature verification, fail closed."""

import base64
import hashlib
import hmac
import json
import time
from datetime import timedelta

import pytest
from django.utils import timezone

from core.models import Contact, ContactChannel, Delivery, Event, Invitation

SECRET_BYTES = b"super-secret-webhook-key-32bytes"
SECRET = "whsec_" + base64.b64encode(SECRET_BYTES).decode()

State = Invitation.State
URL = "/webhooks/resend"


def sign(body: bytes, *, key: bytes = SECRET_BYTES, msg_id="msg_1", timestamp=None):
    timestamp = str(timestamp or int(time.time()))
    digest = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return {
        "svix-id": msg_id,
        "svix-timestamp": timestamp,
        "svix-signature": "v1," + base64.b64encode(digest).decode(),
    }


def bounce_payload(provider_id: str) -> bytes:
    return json.dumps({"type": "email.bounced", "data": {"email_id": provider_id}}).encode()


@pytest.fixture
def secret(settings):
    settings.RESEND_WEBHOOK_SECRET = SECRET


@pytest.fixture
def sent_delivery(db):
    event = Event.objects.create(
        title="BBQ", starts_at=timezone.now() + timedelta(days=7), status=Event.Status.ACTIVE
    )
    contact = Contact.objects.create(name="Alex")
    channel = ContactChannel.objects.create(
        contact=contact, kind=ContactChannel.Kind.EMAIL, value="a@x.com"
    )
    invitation = Invitation.objects.create(event=event, contact=contact)
    invitation.advance_state(State.SENT)
    return Delivery.objects.create(
        invitation=invitation,
        channel=channel,
        kind=ContactChannel.Kind.EMAIL,
        address_used="a@x.com",
        status=Delivery.Status.SENT,
        provider_message_id="re_123",
    )


def post(client, body: bytes, headers: dict):
    return client.post(URL, data=body, content_type="application/json", headers=headers)


def test_valid_bounce_flips_delivery_and_invitation(client, secret, sent_delivery):
    body = bounce_payload("re_123")
    resp = post(client, body, sign(body))
    assert resp.status_code == 200

    sent_delivery.refresh_from_db()
    assert sent_delivery.status == Delivery.Status.BOUNCED
    assert sent_delivery.invitation.state == State.BOUNCED


def test_bounce_cannot_regress_an_opened_invitation(client, secret, sent_delivery):
    invitation = sent_delivery.invitation
    invitation.advance_state(State.OPENED)

    body = bounce_payload("re_123")
    post(client, body, sign(body))

    sent_delivery.refresh_from_db()
    invitation.refresh_from_db()
    assert sent_delivery.status == Delivery.Status.BOUNCED  # audit records the bounce
    assert invitation.state == State.OPENED  # ladder holds (§2.3)


def test_forged_signature_rejected(client, secret, sent_delivery):
    body = bounce_payload("re_123")
    resp = post(client, body, sign(body, key=b"wrong-key-entirely-000000000000"))
    assert resp.status_code == 403
    sent_delivery.refresh_from_db()
    assert sent_delivery.status == Delivery.Status.SENT  # untouched


def test_missing_headers_rejected(client, secret, sent_delivery):
    assert post(client, bounce_payload("re_123"), {}).status_code == 403


def test_stale_timestamp_rejected(client, secret, sent_delivery):
    body = bounce_payload("re_123")
    old = int(time.time()) - 3600
    assert post(client, body, sign(body, timestamp=old)).status_code == 403


def test_unset_secret_fails_closed(client, sent_delivery, settings):
    settings.RESEND_WEBHOOK_SECRET = ""
    body = bounce_payload("re_123")
    assert post(client, body, sign(body)).status_code == 403


def test_unknown_email_id_is_acked(client, secret, db):
    body = bounce_payload("re_does_not_exist")
    assert post(client, body, sign(body)).status_code == 200


def test_irrelevant_event_types_ignored(client, secret, sent_delivery):
    body = json.dumps({"type": "email.delivered", "data": {"email_id": "re_123"}}).encode()
    assert post(client, body, sign(body)).status_code == 200
    sent_delivery.refresh_from_db()
    assert sent_delivery.status == Delivery.Status.SENT
