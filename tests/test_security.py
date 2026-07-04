"""Phase 7 security pass (§8) + PWA plumbing (§7): CSP on every response, no
inline JS anywhere, token redaction in logs, service worker + manifest wiring."""

import logging
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import Contact, Event, Invitation
from core.security import RedactTokenFilter

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_client(client, django_user_model):
    user = django_user_model.objects.create_superuser("sam", "sam@example.com", "pw-strong-123")
    client.force_login(user)
    return client


@pytest.fixture
def invitation(db):
    event = Event.objects.create(
        title="Summer BBQ",
        starts_at=timezone.now() + timedelta(days=7),
        status=Event.Status.ACTIVE,
    )
    return Invitation.objects.create(event=event, contact=Contact.objects.create(name="Dave"))


# --------------------------------------------------------------------------- #
#  CSP (§8 item 2)
# --------------------------------------------------------------------------- #
def test_guest_page_gets_strict_csp(client, invitation):
    csp = client.get(invitation.rsvp_path)["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp


def test_organizer_pages_get_strict_csp(staff_client, invitation):
    for name in ("event-dashboard", "event-send", "event-queue"):
        csp = staff_client.get(reverse(name, args=[invitation.event.pk]))["Content-Security-Policy"]
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp


def test_django_admin_gets_relaxed_style_csp_only(staff_client):
    csp = staff_client.get("/admin/")["Content-Security-Policy"]
    assert "style-src 'self' 'unsafe-inline'" in csp  # admin widgets still inline styles
    assert "script-src 'self'" in csp  # but scripts stay external even there


def test_no_inline_js_in_rendered_pages(staff_client, invitation):
    pages = [
        staff_client.get(invitation.rsvp_path).content.decode(),
        staff_client.get(reverse("event-dashboard", args=[invitation.event.pk])).content.decode(),
        staff_client.get(reverse("event-send", args=[invitation.event.pk])).content.decode(),
        staff_client.get(reverse("event-queue", args=[invitation.event.pk])).content.decode(),
    ]
    for page in pages:
        assert "onclick=" not in page and "onsubmit=" not in page
        assert "<script>" not in page  # only <script src=...> allowed


# --------------------------------------------------------------------------- #
#  Token redaction (§8 item 3)
# --------------------------------------------------------------------------- #
def test_request_logs_redact_capability_tokens(invitation):
    record = logging.LogRecord(
        "django.request",
        logging.WARNING,
        __file__,
        0,
        "Not Found: /i/%s",
        (invitation.token,),
        None,
    )
    assert RedactTokenFilter().filter(record) is True
    assert invitation.token not in record.getMessage()
    assert "/i/[token]" in record.getMessage()


def test_redaction_filter_is_wired_to_django_request_logger():
    filters = logging.getLogger("django.request").filters
    assert any(isinstance(f, RedactTokenFilter) for f in filters)


def test_short_paths_pass_through_untouched():
    record = logging.LogRecord(
        "django.request", logging.WARNING, __file__, 0, "Not Found: /i/junk", None, None
    )
    RedactTokenFilter().filter(record)
    assert record.getMessage() == "Not Found: /i/junk"  # too short to be a token


# --------------------------------------------------------------------------- #
#  PWA (§7)
# --------------------------------------------------------------------------- #
def test_service_worker_served_under_admin_scope(client):
    response = client.get("/admin/sw.js")
    assert response.status_code == 200
    assert "javascript" in response["Content-Type"]
    assert response["Cache-Control"] == "no-cache"
    assert b"evently-static" in response.content


def test_organizer_pages_wire_up_the_pwa(staff_client, invitation):
    page = staff_client.get(reverse("event-dashboard", args=[invitation.event.pk])).content.decode()
    assert 'rel="manifest"' in page
    assert 'data-sw="/admin/sw.js"' in page
    assert "apple-touch-icon" in page


def test_guest_page_is_not_the_pwa(client, invitation):
    page = client.get(invitation.rsvp_path).content.decode()
    assert 'rel="manifest"' not in page and "data-sw" not in page
