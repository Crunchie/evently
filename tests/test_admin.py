import pytest
from django.contrib import admin
from django.urls import reverse


@pytest.mark.django_db
def test_admin_changelists_load(client, django_user_model):
    """Every registered admin renders — proves the registrations are valid."""
    user = django_user_model.objects.create_superuser("admin", "admin@example.com", "pw-strong-123")
    client.force_login(user)
    for model in admin.site._registry:
        url = reverse(f"admin:{model._meta.app_label}_{model._meta.model_name}_changelist")
        assert client.get(url).status_code == 200, url


@pytest.mark.django_db
def test_invitation_add_form_with_attendee_inline(client, django_user_model):
    user = django_user_model.objects.create_superuser("admin2", "a2@example.com", "pw-strong-123")
    client.force_login(user)
    assert client.get(reverse("admin:core_invitation_add")).status_code == 200
