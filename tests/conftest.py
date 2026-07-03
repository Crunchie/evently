import pytest


@pytest.fixture(autouse=True)
def _plain_static_storage(settings):
    """Tests run with DEBUG=False, which selects the manifest static storage — but no
    manifest exists outside the Docker build (collectstatic). Use the plain storage so
    admin templates' {% static %} calls resolve without a manifest."""
    settings.STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
