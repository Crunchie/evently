from django.conf import settings


def build_id(request):
    """Expose the image build stamp to templates so the organizer PWA can stamp its pages
    and detect when a new version has been deployed (§7). See settings.BUILD_ID."""
    return {"build_id": settings.BUILD_ID}
