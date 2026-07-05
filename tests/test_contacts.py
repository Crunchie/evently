"""Hand-built Contacts & Households flow (§2.2): the add-contact and create-household
organizer views that replace the Django admin for the everyday job. No network anywhere —
channel validation is local (email + phonenumbers). Mirrors the staff_client pattern."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import Contact, ContactChannel, Event, Household

Kind = ContactChannel.Kind
Status = ContactChannel.Status


@pytest.fixture
def staff_client(client, django_user_model):
    user = django_user_model.objects.create_superuser("sam", "sam@example.com", "pw-strong-123")
    client.force_login(user)
    return client


# --- Organizer home --------------------------------------------------------- #
@pytest.mark.django_db
def test_admin_home_lists_events_and_links(staff_client):
    soon = Event.objects.create(
        title="Summer BBQ", starts_at=timezone.now() + timedelta(days=7), status=Event.Status.ACTIVE
    )
    Event.objects.create(
        title="Old Party", starts_at=timezone.now() - timedelta(days=7), status=Event.Status.ACTIVE
    )
    body = staff_client.get(reverse("admin-home")).content.decode()
    assert "Summer BBQ" in body  # upcoming
    assert "Old Party" in body  # earlier
    assert reverse("contacts-home") in body  # jump to contacts
    assert reverse("event-dashboard", args=[soon.pk]) in body  # event → dashboard
    assert reverse("admin:index") in body  # link to the full admin site


@pytest.mark.django_db
def test_admin_index_links_to_organizer_home(staff_client):
    """Our core/templates/admin/index.html override must win over Django's, so /admin
    surfaces the home (proves the INSTALLED_APPS ordering took effect)."""
    body = staff_client.get(reverse("admin:index")).content.decode()
    assert reverse("admin-home") in body
    assert "Organizer home" in body


@pytest.mark.django_db
def test_admin_home_requires_staff(client):
    resp = client.get(reverse("admin-home"))
    assert resp.status_code == 302 and "/admin/login" in resp.url


# --- Contacts hub ----------------------------------------------------------- #
@pytest.mark.django_db
def test_contacts_home_lists_households_and_loose(staff_client):
    hh = Household.objects.create(name="The Hendersons")
    Contact.objects.create(name="Dave Henderson", household=hh)
    Contact.objects.create(name="Solo Sally")
    body = staff_client.get(reverse("contacts-home")).content.decode()
    assert "The Hendersons" in body
    assert "Dave Henderson" in body
    assert "Solo Sally" in body


@pytest.mark.django_db
def test_contacts_home_search_filters(staff_client):
    Contact.objects.create(name="Alice Zephyr")
    Contact.objects.create(name="Bob Yarrow")
    body = staff_client.get(reverse("contacts-home"), {"q": "zephyr"}).content.decode()
    assert "Alice Zephyr" in body
    assert "Bob Yarrow" not in body


@pytest.mark.django_db
def test_form_pages_render(staff_client):
    """Every add/edit page renders its repeatable-row scaffold (GET), incl. edit views."""
    hh = Household.objects.create(name="The Renders")
    contact = Contact.objects.create(name="Rendell", household=hh)
    for name, args, marker in [
        ("contact-new", [], b"data-row-template"),
        ("contact-edit", [contact.pk], b"Rendell"),
        ("household-new", [], b"data-row-template"),
        ("household-edit", [hh.pk], b"The Renders"),
    ]:
        resp = staff_client.get(reverse(name, args=args))
        assert resp.status_code == 200 and marker in resp.content, name


# --- Add a contact ---------------------------------------------------------- #
@pytest.mark.django_db
def test_contact_new_creates_with_channels_and_tags(staff_client):
    resp = staff_client.post(
        reverse("contact-new"),
        {
            "name": "Dave",
            "nickname": "Davey",
            "birth_year": "1990",
            "household": "",
            "notes": "likes pavlova",
            "tags": "family, book club",
            "channel_id": ["", ""],
            "channel_kind": ["email", "whatsapp"],
            "channel_value": ["dave@example.com", "021 123 4567"],
            "channel_label": ["", "mobile"],
            "channel_delete": ["0", "0"],
            "preferred": "1",  # the WhatsApp row
        },
    )
    assert resp.status_code == 302
    dave = Contact.objects.get(name="Dave")
    assert dave.nickname == "Davey"
    assert dave.birth_year == 1990
    assert set(dave.tags.values_list("name", flat=True)) == {"family", "book club"}
    channels = {c.kind: c for c in dave.channels.all()}
    assert channels[Kind.EMAIL].value == "dave@example.com"
    assert channels[Kind.WHATSAPP].value == "+64211234567"  # normalized to E.164
    assert channels[Kind.WHATSAPP].is_preferred and not channels[Kind.EMAIL].is_preferred


@pytest.mark.django_db
def test_contact_new_rejects_bad_email_without_writing(staff_client):
    resp = staff_client.post(
        reverse("contact-new"),
        {
            "name": "Bad",
            "nickname": "",
            "birth_year": "",
            "household": "",
            "notes": "",
            "tags": "",
            "channel_id": [""],
            "channel_kind": ["email"],
            "channel_value": ["not-an-email"],
            "channel_label": [""],
            "channel_delete": ["0"],
            "preferred": "0",
        },
    )
    assert resp.status_code == 200  # re-rendered with error, no redirect
    assert "valid email" in resp.content.decode()
    assert not Contact.objects.filter(name="Bad").exists()  # nothing written


@pytest.mark.django_db
def test_contact_new_messenger_needs_no_value(staff_client):
    staff_client.post(
        reverse("contact-new"),
        {
            "name": "Mo",
            "nickname": "",
            "birth_year": "",
            "household": "",
            "notes": "",
            "tags": "",
            "channel_id": [""],
            "channel_kind": ["messenger"],
            "channel_value": [""],
            "channel_label": [""],
            "channel_delete": ["0"],
            "preferred": "0",
        },
    )
    mo = Contact.objects.get(name="Mo")
    ch = mo.channels.get()
    assert ch.kind == Kind.MESSENGER and ch.value == "" and ch.is_preferred


@pytest.mark.django_db
def test_contact_new_requires_name(staff_client):
    resp = staff_client.post(reverse("contact-new"), {"name": "", "channel_kind": []})
    assert resp.status_code == 200
    assert Contact.objects.count() == 0


# --- Edit a contact --------------------------------------------------------- #
@pytest.mark.django_db
def test_contact_edit_diffs_channels_and_keeps_proposed(staff_client):
    contact = Contact.objects.create(name="Ed")
    keep = ContactChannel.objects.create(contact=contact, kind=Kind.EMAIL, value="old@x.com")
    drop = ContactChannel.objects.create(contact=contact, kind=Kind.WHATSAPP, value="+64211234567")
    # A guest-requested channel awaiting approval must survive an organizer edit (§2.5).
    proposed = ContactChannel.objects.create(
        contact=contact,
        kind=Kind.EMAIL,
        value="guest@x.com",
        status=Status.PROPOSED,
        source=ContactChannel.Source.GUEST,
    )
    resp = staff_client.post(
        reverse("contact-edit", args=[contact.pk]),
        {
            "name": "Ed",
            "nickname": "",
            "birth_year": "",
            "household": "",
            "notes": "",
            "tags": "",
            # keep (updated) + drop (delete flag) + a brand-new row
            "channel_id": [str(keep.pk), str(drop.pk), ""],
            "channel_kind": ["email", "whatsapp", "sms"],
            "channel_value": ["new@x.com", "+64211234567", "021 123 4567"],
            "channel_label": ["", "", ""],
            "channel_delete": ["0", "1", "0"],
            "preferred": "2",  # the new SMS row
        },
    )
    assert resp.status_code == 302
    keep.refresh_from_db()
    assert keep.value == "new@x.com"  # updated in place, id preserved
    assert not ContactChannel.objects.filter(pk=drop.pk).exists()  # delete-flagged → gone
    assert ContactChannel.objects.filter(
        pk=proposed.pk, status=Status.PROPOSED
    ).exists()  # untouched
    active = contact.channels.filter(status=Status.ACTIVE)
    assert active.filter(is_preferred=True).count() == 1  # single-preferred holds
    assert active.get(is_preferred=True).kind == Kind.SMS


# --- Create a household ----------------------------------------------------- #
@pytest.mark.django_db
def test_household_new_creates_members_channels_and_primary(staff_client):
    resp = staff_client.post(
        reverse("household-new"),
        {
            "name": "The Hendersons",
            "member_name": ["Dave Henderson", "Kate Henderson", "Milo", ""],
            "member_nick": ["Dave", "", "", ""],
            "member_birth": ["", "", "2016", ""],
            "member_ch_kind": ["whatsapp", "email", "", ""],
            "member_ch_value": ["021 123 4567", "kate@x.com", "", ""],
            "member_delete": ["0", "0", "0", "0"],
            "primary": "1",  # Kate
        },
    )
    assert resp.status_code == 302
    hh = Household.objects.get(name="The Hendersons")
    members = {c.name: c for c in hh.members.all()}
    assert set(members) == {"Dave Henderson", "Kate Henderson", "Milo"}  # blank row skipped
    assert (
        members["Milo"].birth_year == 2016 and not members["Milo"].channels.exists()
    )  # kid, no channel
    assert members["Dave Henderson"].channels.get().value == "+64211234567"  # E.164
    assert members["Kate Henderson"].channels.get().is_preferred
    assert hh.primary_contact == members["Kate Henderson"]  # radio honored


@pytest.mark.django_db
def test_household_new_defaults_primary_to_first_member(staff_client):
    staff_client.post(
        reverse("household-new"),
        {
            "name": "Nomads",
            "member_name": ["First", "Second"],
            "member_nick": ["", ""],
            "member_birth": ["", ""],
            "member_ch_kind": ["", ""],
            "member_ch_value": ["", ""],
            "member_delete": ["0", "0"],
            # no "primary" posted
        },
    )
    hh = Household.objects.get(name="Nomads")
    assert hh.primary_contact.name == "First"


@pytest.mark.django_db
def test_household_new_requires_a_member(staff_client):
    resp = staff_client.post(
        reverse("household-new"),
        {"name": "Empty", "member_name": [""], "member_delete": ["0"]},
    )
    assert resp.status_code == 200
    assert not Household.objects.filter(name="Empty").exists()


@pytest.mark.django_db
def test_household_new_bad_phone_rejected(staff_client):
    resp = staff_client.post(
        reverse("household-new"),
        {
            "name": "Oops",
            "member_name": ["Kid"],
            "member_nick": [""],
            "member_birth": [""],
            "member_ch_kind": ["whatsapp"],
            "member_ch_value": ["nonsense"],
            "member_delete": ["0"],
            "primary": "0",
        },
    )
    assert resp.status_code == 200
    assert not Household.objects.filter(name="Oops").exists()


# --- Edit a household ------------------------------------------------------- #
@pytest.mark.django_db
def test_household_edit_renames_and_sets_primary(staff_client):
    hh = Household.objects.create(name="Old Name")
    a = Contact.objects.create(name="A", household=hh)
    b = Contact.objects.create(name="B", household=hh)
    hh.primary_contact = a
    hh.save()
    resp = staff_client.post(
        reverse("household-edit", args=[hh.pk]), {"name": "New Name", "primary": str(b.pk)}
    )
    assert resp.status_code == 302
    hh.refresh_from_db()
    assert hh.name == "New Name" and hh.primary_contact == b


# --- Auth ------------------------------------------------------------------- #
@pytest.mark.django_db
@pytest.mark.parametrize("name", ["contacts-home", "contact-new", "household-new"])
def test_contacts_views_require_staff(client, name):
    resp = client.get(reverse(name))
    assert resp.status_code == 302  # staff_member_required bounces anonymous users to login
    assert "/admin/login" in resp.url
