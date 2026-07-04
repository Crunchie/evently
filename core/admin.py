"""Organizer backoffice (§2.6). v1 leans on the Django admin for CRUD; the polished
dashboard + send queue are hand-built later (Phase 6)."""

from django.contrib import admin
from django.db.models import Count
from django.urls import reverse
from django.utils.html import format_html

from .models import (
    Contact,
    ContactChannel,
    Delivery,
    Event,
    Household,
    Invitation,
    InvitationAttendee,
    RsvpEvent,
    Tag,
)


# --- Inlines ---------------------------------------------------------------- #
class ContactChannelInline(admin.TabularInline):
    model = ContactChannel
    fk_name = "contact"
    extra = 0
    fields = ("kind", "value", "label", "is_preferred", "status", "source")


class HouseholdMemberInline(admin.TabularInline):
    model = Contact
    fk_name = "household"
    extra = 0
    fields = ("name", "nickname", "birth_year")
    verbose_name = "member"
    verbose_name_plural = "members"


class InvitationAttendeeInline(admin.TabularInline):
    # Rows are auto-created from the envelope's target (Invitation.sync_attendees) —
    # here they're edited, never added (avoids duplicate-contact errors) and never
    # deleted (history is retained; uninvite = revoke the invitation, §2.2).
    model = InvitationAttendee
    extra = 0
    fields = ("contact", "rsvp_status", "responded_at")
    readonly_fields = ("contact",)
    can_delete = False

    def has_add_permission(self, request, obj):
        return False


# --- Contacts / households / channels --------------------------------------- #
@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(Household)
class HouseholdAdmin(admin.ModelAdmin):
    list_display = ("name", "primary_contact", "member_count")
    search_fields = ("name",)
    list_select_related = ("primary_contact",)
    inlines = [HouseholdMemberInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_member_count=Count("members"))

    @admin.display(description="members", ordering="_member_count")
    def member_count(self, obj):
        return obj._member_count


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("name", "nickname", "household", "preferred_channel")
    list_filter = ("tags", "household")
    search_fields = ("name", "nickname")
    list_select_related = ("household",)
    filter_horizontal = ("tags",)
    inlines = [ContactChannelInline]


@admin.register(ContactChannel)
class ContactChannelAdmin(admin.ModelAdmin):
    # Standalone view is handy for the channel-change approval queue (status=proposed).
    list_display = ("contact", "kind", "value", "is_preferred", "status", "source")
    list_filter = ("status", "kind", "source")
    search_fields = ("contact__name", "value")
    list_select_related = ("contact",)


# --- Events / invitations --------------------------------------------------- #
@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("title", "starts_at", "status", "expected_headcount", "dashboard_link")
    list_filter = ("status",)
    search_fields = ("title",)
    date_hierarchy = "starts_at"
    readonly_fields = ("ics_uid", "created_at", "updated_at")

    @admin.display(description="dashboard")
    def dashboard_link(self, obj):
        return format_html('<a href="{}">open ↗</a>', reverse("event-dashboard", args=[obj.pk]))


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("event", "target", "state", "plus_ones", "opened_at")
    list_filter = ("state", "event")
    search_fields = ("contact__name", "household__name", "token")
    list_select_related = ("event", "contact", "household")
    readonly_fields = ("token", "created_at", "updated_at")
    inlines = [InvitationAttendeeInline]

    @admin.display(description="target")
    def target(self, obj):
        return obj.household or obj.contact

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Household membership may have changed since creation — top up missing rows.
        form.instance.sync_attendees()


@admin.register(InvitationAttendee)
class InvitationAttendeeAdmin(admin.ModelAdmin):
    list_display = ("invitation", "contact", "rsvp_status")
    list_filter = ("rsvp_status",)
    search_fields = ("contact__name",)
    # __str__ of invitation touches event + contact/household — avoid N+1 per row.
    list_select_related = (
        "contact",
        "invitation__event",
        "invitation__contact",
        "invitation__household",
    )


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = ("invitation", "kind", "status", "sent_at")
    list_filter = ("status", "kind")
    list_select_related = ("invitation__event", "invitation__contact", "invitation__household")


@admin.register(RsvpEvent)
class RsvpEventAdmin(admin.ModelAdmin):
    # Append-only history — view only: no add, change, or delete.
    list_display = ("attendee", "status", "actor", "actor_user", "created_at")
    list_filter = ("actor", "status")
    list_select_related = ("attendee__contact", "actor_user")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
