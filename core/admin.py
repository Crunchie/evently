"""Organizer backoffice (§2.6). v1 leans on the Django admin for CRUD; the polished
dashboard + send queue are hand-built later (Phase 6)."""

from django.contrib import admin

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
    model = InvitationAttendee
    extra = 0
    fields = ("contact", "rsvp_status", "responded_at")


# --- Contacts / households / channels --------------------------------------- #
@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name",)
    search_fields = ("name",)


@admin.register(Household)
class HouseholdAdmin(admin.ModelAdmin):
    list_display = ("name", "primary_contact", "member_count")
    search_fields = ("name",)
    inlines = [HouseholdMemberInline]

    @admin.display(description="members")
    def member_count(self, obj):
        return obj.members.count()


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ("name", "nickname", "household", "preferred_channel")
    list_filter = ("tags", "household")
    search_fields = ("name", "nickname")
    filter_horizontal = ("tags",)
    inlines = [ContactChannelInline]


@admin.register(ContactChannel)
class ContactChannelAdmin(admin.ModelAdmin):
    # Standalone view is handy for the channel-change approval queue (status=proposed).
    list_display = ("contact", "kind", "value", "is_preferred", "status", "source")
    list_filter = ("status", "kind", "source")
    search_fields = ("contact__name", "value")


# --- Events / invitations --------------------------------------------------- #
@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ("title", "starts_at", "status", "expected_headcount")
    list_filter = ("status",)
    search_fields = ("title",)
    date_hierarchy = "starts_at"
    readonly_fields = ("ics_uid", "created_at", "updated_at")


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("event", "target", "state", "plus_ones", "opened_at")
    list_filter = ("state", "event")
    search_fields = ("contact__name", "household__name", "token")
    readonly_fields = ("token", "created_at", "updated_at")
    inlines = [InvitationAttendeeInline]

    @admin.display(description="target")
    def target(self, obj):
        return obj.household or obj.contact


@admin.register(InvitationAttendee)
class InvitationAttendeeAdmin(admin.ModelAdmin):
    list_display = ("invitation", "contact", "rsvp_status")
    list_filter = ("rsvp_status",)
    search_fields = ("contact__name",)


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = ("invitation", "kind", "status", "sent_at")
    list_filter = ("status", "kind")


@admin.register(RsvpEvent)
class RsvpEventAdmin(admin.ModelAdmin):
    # Append-only history — view only, no add/delete.
    list_display = ("attendee", "status", "actor", "actor_user", "created_at")
    list_filter = ("actor", "status")
    readonly_fields = ("attendee", "status", "note", "actor", "actor_user", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
