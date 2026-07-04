"""
Events app — core data model (Django).

Concrete models translating the conceptual model in "Events App — Design Summary.md" §5
into Django. Notes:

- **Single-tenant / self-hosted (§8).** This is one household's private instance (you +
  co-host). All organizers share one dataset; there is no per-owner access scoping.
  `created_by` is recorded for attribution ("who made this"), not authorization.
- **Envelope vs attendee (§5).** `Invitation` is the envelope (one token/link, targets a
  contact XOR a household); `InvitationAttendee` is each counted person. A single-contact
  invite is just an envelope with one attendee — no special-casing.
- **Plus-ones live on the envelope** (`Invitation.plus_ones`), not per attendee — matches
  the RSVP-page UX (one stepper per household/guest) and keeps the headcount simple.
- Target DB is SQLite in WAL mode (§9); everything here is plain relational SQL.
"""

import secrets
import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone


def make_token() -> str:
    """~256-bit URL-safe capability token for an invitation link (§8)."""
    return secrets.token_urlsafe(32)


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# --------------------------------------------------------------------------- #
#  Contacts, households, channels
# --------------------------------------------------------------------------- #
class Tag(models.Model):
    """A label for bulk guest-list operations ("family", "book club") — §2.2."""

    name = models.CharField(max_length=50, unique=True)

    def __str__(self):
        return self.name


class Household(TimestampedModel):
    """A family/group invited as one unit with one link (§2.2)."""

    name = models.CharField(max_length=120)  # e.g. "The Hendersons"
    # The known "main" person of the household (for fallback greetings/attribution).
    primary_contact = models.ForeignKey(
        "Contact", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    def __str__(self):
        return self.name


class Contact(TimestampedModel):
    """A person you might invite. Not a user account — most invitees never sign up."""

    name = models.CharField(max_length=120)
    nickname = models.CharField(max_length=60, blank=True)  # used in greetings if set
    household = models.ForeignKey(
        Household, null=True, blank=True, on_delete=models.SET_NULL, related_name="members"
    )
    # Optional; drives the "age N" hint for kids on the household RSVP page (§2.5).
    birth_year = models.PositiveSmallIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)
    tags = models.ManyToManyField(Tag, blank=True, related_name="contacts")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )

    def __str__(self):
        return self.name

    @property
    def preferred_channel(self):
        """The active channel used by default when inviting this contact (§2.2)."""
        return self.channels.filter(is_preferred=True, status=ContactChannel.Status.ACTIVE).first()

    @property
    def greeting_name(self) -> str:
        """Name used in guest-facing greetings (§2.5): nickname, else first name."""
        parts = self.name.split()
        return self.nickname or (parts[0] if parts else self.name)


class ContactChannel(TimestampedModel):
    """
    A way to reach a contact. A guest-requested channel change (§2.5) is just a row with
    status=PROPOSED + source=GUEST; the organizer's approval flips it to ACTIVE + preferred.
    The dashboard approval queue is `ContactChannel.objects.filter(status=PROPOSED)`.
    """

    class Kind(models.TextChoices):
        EMAIL = "email", "Email"
        WHATSAPP = "whatsapp", "WhatsApp"
        MESSENGER = "messenger", "Messenger"
        SMS = "sms", "SMS"
        TELEGRAM = "telegram", "Telegram"

    class Source(models.TextChoices):
        ORGANIZER = "organizer", "Organizer-entered"
        GUEST = "guest_requested", "Guest-requested"

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        PROPOSED = "proposed", "Proposed (awaiting approval)"

    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="channels")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    # Email address, or E.164 phone for WhatsApp/SMS. Blank for Messenger (assisted, no
    # address needed). Validate "required unless MESSENGER" in the form layer.
    value = models.CharField(max_length=255, blank=True)
    label = models.CharField(max_length=60, blank=True)  # e.g. "work email"
    is_preferred = models.BooleanField(default=False)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.ORGANIZER)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    # Which invitation the guest was looking at when they requested this (audit/context).
    requested_via = models.ForeignKey(
        "Invitation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="channel_requests",
    )

    class Meta:
        constraints = [
            # At most one preferred channel per contact (partial unique — SQLite supports it).
            models.UniqueConstraint(
                fields=["contact"],
                condition=Q(is_preferred=True),
                name="one_preferred_channel_per_contact",
            ),
        ]

    def __str__(self):
        return f"{self.contact} · {self.get_kind_display()}"


# --------------------------------------------------------------------------- #
#  Events
# --------------------------------------------------------------------------- #
class Event(TimestampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        ACTIVE = "active", "Active"
        CANCELLED = "cancelled", "Cancelled"

    title = models.CharField(max_length=200)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    location_text = models.CharField(max_length=255, blank=True)
    location_url = models.URLField(blank=True)  # optional map link
    description = models.TextField(blank=True)
    host_display = models.CharField(max_length=120, blank=True)  # "Sam & Kate"
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    # Per-event toggles (§2.1)
    allow_plus_ones = models.BooleanField(default=True)
    plus_ones_cap = models.PositiveSmallIntegerField(null=True, blank=True)  # optional cap
    show_guest_list = models.BooleanField(default=False)  # first names of "going" only

    # Stable UID for the outbound add-to-calendar .ics, so re-adding dedupes (§5).
    ics_uid = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="events",
    )

    class Meta:
        indexes = [models.Index(fields=["status", "starts_at"])]

    def __str__(self):
        return self.title

    @property
    def is_past(self) -> bool:
        return self.starts_at < timezone.now()

    @property
    def expected_headcount(self) -> int:
        """
        The number to cater for (§2.6): every attendee marked Going (individuals AND
        household members) + plus-ones on envelopes that have at least one Going
        attendee. Revoked envelopes don't count — uninviting someone (§2.2) removes
        them from the catering number even though their RSVP history is retained.
        """
        going = (
            InvitationAttendee.objects.filter(
                invitation__event=self, rsvp_status=InvitationAttendee.Rsvp.GOING
            )
            .exclude(invitation__state=Invitation.State.REVOKED)
            .count()
        )
        plus = (
            self.invitations.filter(attendees__rsvp_status=InvitationAttendee.Rsvp.GOING)
            .exclude(state=Invitation.State.REVOKED)
            .distinct()
            .aggregate(total=Sum("plus_ones"))["total"]
            or 0
        )
        return going + plus


# --------------------------------------------------------------------------- #
#  Invitations (envelope) + attendees (counted people)
# --------------------------------------------------------------------------- #
class Invitation(TimestampedModel):
    """The envelope: one token/link, targets exactly one contact OR one household."""

    class State(models.TextChoices):
        PENDING = "pending", "Pending"  # created, not sent
        QUEUED = "queued", "Queued"  # picked up by the delivery worker
        SENT = "sent", "Sent"  # automated: provider accepted
        SHARED = "shared", "Shared"  # assisted: share/deep-link invoked (optimistic)
        OPENED = "opened", "Opened"  # first link click — the real delivery signal
        RESPONDED = "responded", "Responded"
        BOUNCED = "bounced", "Bounced"
        # Uninvited (§2.2): soft-revoke, never delete — deleting the invitation would
        # CASCADE through attendees → rsvp_events and destroy history the design says
        # to retain. The link shows the soft "no longer available" page.
        REVOKED = "revoked", "Revoked"

    # Monotonic ladder rank (§2.3): the envelope only moves forward. SENT and SHARED are
    # equivalent progress ("it went out"); BOUNCED sits between going-out and OPENED so a
    # bounce can't override an open, and a later open (another channel/household copy)
    # clears a bounce. REVOKED is terminal and handled separately in advance_state().
    STATE_RANK = {
        State.PENDING: 0,
        State.QUEUED: 1,
        State.SENT: 2,
        State.SHARED: 2,
        State.BOUNCED: 3,
        State.OPENED: 4,
        State.RESPONDED: 5,
    }

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="invitations")
    contact = models.ForeignKey(
        Contact, null=True, blank=True, on_delete=models.CASCADE, related_name="invitations"
    )
    household = models.ForeignKey(
        Household, null=True, blank=True, on_delete=models.CASCADE, related_name="invitations"
    )

    token = models.CharField(max_length=64, unique=True, default=make_token, editable=False)
    # The channel chosen for the primary send (override of the contact's preferred).
    # Households may deliver to several member channels — see Delivery rows.
    send_channel = models.ForeignKey(
        ContactChannel, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    state = models.CharField(max_length=20, choices=State.choices, default=State.PENDING)
    opened_at = models.DateTimeField(null=True, blank=True)  # first link click

    plus_ones = models.PositiveSmallIntegerField(default=0)  # envelope-level (§5 refinement)
    latest_note = models.TextField(blank=True)  # denormalized most-recent note
    latest_note_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # Exactly one target: a contact XOR a household.
            models.CheckConstraint(
                name="invitation_contact_xor_household",
                condition=(
                    Q(contact__isnull=False, household__isnull=True)
                    | Q(contact__isnull=True, household__isnull=False)
                ),
            ),
            # No double-inviting the same contact/household to the same event.
            models.UniqueConstraint(
                fields=["event", "contact"],
                condition=Q(contact__isnull=False),
                name="one_invitation_per_event_contact",
            ),
            models.UniqueConstraint(
                fields=["event", "household"],
                condition=Q(household__isnull=False),
                name="one_invitation_per_event_household",
            ),
        ]
        indexes = [models.Index(fields=["state"])]

    def __str__(self):
        return f"{self.event} → {self.household or self.contact}"

    @property
    def rsvp_path(self) -> str:
        return f"/i/{self.token}"

    @property
    def greeting(self) -> str:
        """Who the RSVP page says hello to: the household, or the person (§2.5)."""
        return self.household.name if self.household_id else self.contact.greeting_name

    @property
    def display_name(self) -> str:
        """Organizer-facing row label. NB: templates must use this, not
        `household.name|default:contact.name` — Django resolves filter *arguments*
        eagerly, and `contact.name` raises on household envelopes (contact is None)."""
        return self.household.name if self.household_id else self.contact.name

    def save(self, *args, **kwargs):
        creating = self._state.adding
        super().save(*args, **kwargs)
        if creating:
            self.sync_attendees()

    def sync_attendees(self) -> None:
        """Ensure one attendee row per covered person (§5): the single contact, or every
        household member. Idempotent — re-run after household membership changes. Never
        removes rows (RSVP history is retained even if someone leaves the household)."""
        people = [self.contact] if self.contact_id else list(self.household.members.all())
        for person in people:
            InvitationAttendee.objects.get_or_create(invitation=self, contact=person)

    def advance_state(self, new_state: str) -> bool:
        """Move the envelope along the monotonic state ladder (§2.3); True if it moved.

        Rules: states only move forward (a link click after responding can't regress
        RESPONDED → OPENED); with several deliveries the envelope reflects the *furthest*
        progress, so a bounce only applies while nothing has been opened, and a later
        open clears a bounce; REVOKED always applies and is terminal.
        """
        if self.state == self.State.REVOKED:
            return False
        if new_state != self.State.REVOKED and (
            self.STATE_RANK[new_state] <= self.STATE_RANK[self.state]
        ):
            return False
        self.state = new_state
        self.save(update_fields=["state", "updated_at"])
        return True


class InvitationAttendee(TimestampedModel):
    """One counted person inside an envelope (the single contact, or each household member)."""

    class Rsvp(models.TextChoices):
        NO_REPLY = "no_reply", "No reply"
        GOING = "going", "Going"
        MAYBE = "maybe", "Maybe"
        CANT = "cant", "Can't"

    invitation = models.ForeignKey(Invitation, on_delete=models.CASCADE, related_name="attendees")
    contact = models.ForeignKey(Contact, on_delete=models.CASCADE, related_name="attendee_records")
    rsvp_status = models.CharField(max_length=10, choices=Rsvp.choices, default=Rsvp.NO_REPLY)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["invitation", "contact"], name="one_attendee_row_per_person"
            ),
        ]

    def __str__(self):
        return f"{self.contact}: {self.get_rsvp_status_display()}"


# --------------------------------------------------------------------------- #
#  Deliveries (outbound queue) + RSVP history
# --------------------------------------------------------------------------- #
class Delivery(TimestampedModel):
    """
    One send attempt over one channel. Doubles as the outbound queue: the cron management
    command (§9) processes rows in status=QUEUED. A household envelope may spawn several.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        SENT = "sent", "Sent"  # automated: provider accepted
        SHARED = "shared", "Shared"  # assisted: share sheet / deep link invoked (optimistic)
        BOUNCED = "bounced", "Bounced"
        FAILED = "failed", "Failed"

    invitation = models.ForeignKey(Invitation, on_delete=models.CASCADE, related_name="deliveries")
    channel = models.ForeignKey(
        ContactChannel, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    kind = models.CharField(max_length=20, choices=ContactChannel.Kind.choices)
    address_used = models.CharField(max_length=255, blank=True)  # snapshot at send time
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.QUEUED)
    provider_message_id = models.CharField(max_length=255, blank=True)  # Resend id, for bounces
    error = models.TextField(blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["status"])]  # queue scan: status=QUEUED

    def __str__(self):
        return f"{self.invitation} · {self.kind} · {self.get_status_display()}"


class RsvpEvent(models.Model):
    """Append-only history of RSVP changes (§5). Current status is denormalized onto attendee."""

    class Actor(models.TextChoices):
        GUEST = "guest", "Guest"
        ORGANIZER = "organizer", "Organizer"

    attendee = models.ForeignKey(
        InvitationAttendee, on_delete=models.CASCADE, related_name="history"
    )
    status = models.CharField(max_length=10, choices=InvitationAttendee.Rsvp.choices)
    note = models.TextField(blank=True)  # note as it stood at this change, if any
    actor = models.CharField(max_length=20, choices=Actor.choices)
    # Which organizer made the change, when actor=ORGANIZER (§2.3 override).
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["attendee", "created_at"])]

    def __str__(self):
        return f"{self.attendee} → {self.status} ({self.actor})"
