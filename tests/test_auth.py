"""Cloudflare Access middleware (core/auth.py). Tokens are signed with a local RSA key
and the JWKS lookup is patched out — no network involved."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth.models import User

from core import auth as core_auth

TEAM = "testteam.cloudflareaccess.com"
AUD = "test-aud-tag"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_WRONG_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_token(email="sam@example.com", aud=AUD, key=_KEY, expires_in=600, omit_email=False):
    now = int(time.time())
    claims = {
        "aud": aud,
        "iss": f"https://{TEAM}",
        "iat": now,
        "exp": now + expires_in,
        "email": email,
    }
    if omit_email:
        del claims["email"]
    return jwt.encode(claims, key, algorithm="RS256")


@pytest.fixture
def access_configured(settings, monkeypatch):
    settings.CF_ACCESS_TEAM_DOMAIN = TEAM
    settings.CF_ACCESS_AUD = AUD
    monkeypatch.setattr(core_auth, "_signing_key", lambda token: _KEY.public_key())


@pytest.mark.django_db
def test_valid_jwt_logs_in_and_creates_organizer(client, access_configured):
    resp = client.get("/admin/", headers={"Cf-Access-Jwt-Assertion": make_token()})
    assert resp.status_code == 200  # admin index, not a login redirect

    user = User.objects.get(username="sam@example.com")
    assert user.is_staff and user.is_superuser


@pytest.mark.django_db
def test_missing_jwt_is_403(client, access_configured):
    assert client.get("/admin/").status_code == 403


@pytest.mark.django_db
def test_bad_signature_is_403(client, access_configured):
    token = make_token(key=_WRONG_KEY)
    resp = client.get("/admin/", headers={"Cf-Access-Jwt-Assertion": token})
    assert resp.status_code == 403
    assert not User.objects.filter(username="sam@example.com").exists()


@pytest.mark.django_db
def test_wrong_audience_is_403(client, access_configured):
    token = make_token(aud="someone-elses-app")
    assert client.get("/admin/", headers={"Cf-Access-Jwt-Assertion": token}).status_code == 403


@pytest.mark.django_db
def test_expired_jwt_is_403(client, access_configured):
    token = make_token(expires_in=-60)
    assert client.get("/admin/", headers={"Cf-Access-Jwt-Assertion": token}).status_code == 403


@pytest.mark.django_db
def test_jwt_without_email_is_403(client, access_configured):
    token = make_token(omit_email=True)
    assert client.get("/admin/", headers={"Cf-Access-Jwt-Assertion": token}).status_code == 403


@pytest.mark.django_db
def test_guest_paths_are_not_gated(client, access_configured):
    assert client.get("/healthz").status_code == 200  # no JWT needed off /admin


@pytest.mark.django_db
def test_unconfigured_falls_back_to_django_login(client, settings):
    settings.CF_ACCESS_TEAM_DOMAIN = ""
    settings.CF_ACCESS_AUD = ""
    resp = client.get("/admin/")
    assert resp.status_code == 302  # normal Django admin login redirect
    assert "/admin/login/" in resp.headers["Location"]


@pytest.mark.django_db
def test_existing_user_is_promoted_to_organizer(client, access_configured):
    User.objects.create_user("kate@example.com", email="kate@example.com")
    token = make_token(email="kate@example.com")
    assert client.get("/admin/", headers={"Cf-Access-Jwt-Assertion": token}).status_code == 200

    user = User.objects.get(username="kate@example.com")
    assert user.is_staff and user.is_superuser
