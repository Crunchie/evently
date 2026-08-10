"""Delivery becomes the outbox (§2.3).

Rows are now written *pending* before anything is sent, so the model grows the message
itself (subject/body + is_edited), a message_kind, and the walkthrough's persisted
deferral. `kind` is **renamed** to `channel_kind` rather than dropped and re-added —
the auto-generated version would have discarded which channel every historic delivery
went out on.

Existing rows are mapped onto the new status vocabulary:
  shared → sent    (an assisted share that had gone out)
  queued → failed  (debris from the old synchronous sender; deliberately *not* pending,
                    which would silently resend an invite the guest may already have)
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def forwards(apps, schema_editor):
    Delivery = apps.get_model("core", "Delivery")
    Delivery.objects.filter(status="shared").update(status="sent")
    Delivery.objects.filter(status="queued").update(
        status="failed", error="left queued by the pre-outbox sender"
    )


def backwards(apps, schema_editor):
    """Best effort: assisted sends were the SHARED ones, and only they can be told apart
    from real email sends after the fact. Rows failed by the forwards mapping stay failed
    — there is no way to know which were the old QUEUED debris."""
    Delivery = apps.get_model("core", "Delivery")
    Delivery.objects.filter(status="sent").exclude(channel_kind="email").update(status="shared")
    Delivery.objects.filter(status__in=("pending", "blocked", "cancelled")).update(status="queued")


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0005_remove_household_primary_contact"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="delivery",
            name="core_delive_status_d4973b_idx",
        ),
        migrations.RenameField(
            model_name="delivery",
            old_name="kind",
            new_name="channel_kind",
        ),
        migrations.AlterField(
            model_name="delivery",
            name="channel_kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("email", "Email"),
                    ("whatsapp", "WhatsApp"),
                    ("messenger", "Messenger"),
                    ("sms", "SMS"),
                    ("telegram", "Telegram"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="delivery",
            name="body",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="delivery",
            name="deferred_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="delivery",
            name="is_edited",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="delivery",
            name="message_kind",
            field=models.CharField(
                blank=True,
                choices=[
                    ("invite", "Invite"),
                    ("nudge", "Nudge"),
                    ("update", "Update"),
                    ("cancellation", "Cancellation"),
                    ("reminder", "Reminder"),
                ],
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="delivery",
            name="sent_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="delivery",
            name="subject",
            field=models.CharField(blank=True, max_length=255),
        ),
        migrations.AlterField(
            model_name="delivery",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("sent", "Sent"),
                    ("failed", "Failed"),
                    ("bounced", "Bounced"),
                    ("blocked", "No channel"),
                    ("cancelled", "Cancelled"),
                ],
                default="pending",
                max_length=20,
            ),
        ),
        # Map old rows onto the new vocabulary *before* the partial unique constraints
        # land, so no legacy row can violate one.
        migrations.RunPython(forwards, backwards),
        migrations.AddIndex(
            model_name="delivery",
            index=models.Index(fields=["status", "message_kind"], name="core_delive_status_8c9712_idx"),
        ),
        migrations.AddConstraint(
            model_name="delivery",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "pending")),
                fields=("invitation", "channel", "message_kind"),
                name="one_pending_message_per_channel",
            ),
        ),
        migrations.AddConstraint(
            model_name="delivery",
            constraint=models.UniqueConstraint(
                condition=models.Q(("channel__isnull", True), ("status", "blocked")),
                fields=("invitation", "message_kind"),
                name="one_blocked_message_per_invitation",
            ),
        ),
    ]
