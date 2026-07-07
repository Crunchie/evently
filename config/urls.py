from django.contrib import admin
from django.urls import path

from core import views

urlpatterns = [
    path("", views.landing, name="landing"),
    path("healthz", views.healthz, name="healthz"),
    path(".well-known/security.txt", views.security_txt, name="security-txt"),
    # Guest side: capability URLs, public (§8).
    path("i/<str:token>", views.rsvp_page, name="rsvp"),
    path("i/<str:token>/calendar.ics", views.rsvp_ics, name="rsvp-ics"),
    path("i/<str:token>/channel", views.rsvp_channel_request, name="rsvp-channel"),
    path("i/<str:token>/poll/<int:poll_pk>", views.rsvp_poll_vote, name="rsvp-poll"),
    path("i/<str:token>/feedback", views.rsvp_feedback, name="rsvp-feedback"),
    # Provider webhooks: public but signature-verified (§8/§9).
    path("webhooks/resend", views.resend_webhook, name="resend-webhook"),
    # Organizer side: everything under /admin so one Access rule gates it all.
    # Custom views must be declared before the admin catch-all.
    path("admin/sw.js", views.service_worker, name="service-worker"),
    # Organizer home — friendly landing behind Access (§2.6).
    path("admin/home/", views.admin_home, name="admin-home"),
    # Contacts & households — hand-built flow (§2.2); admin stays as CRUD backup.
    path("admin/contacts/", views.contacts_home, name="contacts-home"),
    path("admin/contacts/new/", views.contact_new, name="contact-new"),
    path("admin/contacts/<int:pk>/edit/", views.contact_edit, name="contact-edit"),
    path("admin/households/new/", views.household_new, name="household-new"),
    path("admin/households/<int:pk>/edit/", views.household_edit, name="household-edit"),
    path("admin/events/<int:pk>/dashboard/", views.event_dashboard, name="event-dashboard"),
    path("admin/events/<int:pk>/invite/", views.event_invite, name="event-invite"),
    path("admin/events/<int:pk>/send/", views.event_send, name="event-send"),
    path("admin/events/<int:pk>/queue/", views.event_queue, name="event-queue"),
    path("admin/events/<int:pk>/polls/", views.event_poll_create, name="event-poll-create"),
    path("admin/polls/<int:pk>/", views.poll_action, name="poll-action"),
    path("admin/invitations/<int:pk>/action/", views.invitation_action, name="invitation-action"),
    path(
        "admin/invitations/<int:pk>/override/",
        views.invitation_override,
        name="invitation-override",
    ),
    path(
        "admin/channel-requests/<int:pk>/",
        views.channel_request_action,
        name="channel-request-action",
    ),
    path("admin/", admin.site.urls),
]
