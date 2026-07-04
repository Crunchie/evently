"""Basic organizer dashboard (§2.6): staff-gated, counts update as RSVPs land."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import Contact, Event, Invitation


@pytest.fixture
def event(db):
    return Event.objects.create(
        title="Summer BBQ",
        starts_at=timezone.now() + timedelta(days=7),
        status=Event.Status.ACTIVE,
    )


@pytest.fixture
def staff_client(client, django_user_model):
    user = django_user_model.objects.create_superuser("sam", "sam@example.com", "pw-strong-123")
    client.force_login(user)
    return client


def test_dashboard_requires_staff(client, event):
    resp = client.get(reverse("event-dashboard", args=[event.pk]))
    assert resp.status_code == 302  # bounced to admin login


def test_dashboard_counts_update_after_rsvp(staff_client, event):
    inv = Invitation.objects.create(event=event, contact=Contact.objects.create(name="Alex Doe"))
    attendee = inv.attendees.get()

    url = reverse("event-dashboard", args=[event.pk])
    content = staff_client.get(url).content.decode()
    assert "Summer BBQ" in content
    assert inv.token in content  # the copyable link — the hand-delivery flow

    # guest responds through their capability link
    staff_client.post(inv.rsvp_path, {f"status_{attendee.pk}": "going", "plus_ones": "1"})

    content = staff_client.get(url).content.decode()
    assert "Responded" in content
    inv.refresh_from_db()
    assert event.expected_headcount == 2  # 1 going + 1 plus-one, shown as Total expected
