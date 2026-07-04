from django.contrib import admin
from django.urls import path

from core import views

urlpatterns = [
    path("healthz", views.healthz, name="healthz"),
    # Guest side: capability URLs, public (§8).
    path("i/<str:token>", views.rsvp_page, name="rsvp"),
    path("i/<str:token>/calendar.ics", views.rsvp_ics, name="rsvp-ics"),
    # Provider webhooks: public but signature-verified (§8/§9).
    path("webhooks/resend", views.resend_webhook, name="resend-webhook"),
    # Organizer side: everything under /admin so one Access rule gates it all.
    # Custom views must be declared before the admin catch-all.
    path("admin/events/<int:pk>/dashboard/", views.event_dashboard, name="event-dashboard"),
    path("admin/events/<int:pk>/send/", views.event_send, name="event-send"),
    path("admin/", admin.site.urls),
]
