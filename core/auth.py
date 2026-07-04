"""Cloudflare Access → Django auth bridge (§8; CLOUDFLARE_SETUP.md §3).

Access authenticates organizers at the edge and forwards a signed JWT with every
request (`Cf-Access-Jwt-Assertion`). This middleware validates it against the team's
public keys and logs the matching Django user in — Access becomes the only login.
Only organizer paths (`/admin…`) are gated; guest pages stay public by design.

Trust model: this is only sound because the app is reachable *solely* through the
tunnel (no published ports, §8) — otherwise the header could be forged by hitting the
app directly. Local dev: leave CF_ACCESS_* unset and the middleware is inert, falling
back to Django's normal admin login.
"""

import logging
from functools import lru_cache

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.http import HttpResponseForbidden

logger = logging.getLogger(__name__)

PROTECTED_PREFIX = "/admin"


@lru_cache(maxsize=2)
def _jwks_client(team_domain: str) -> jwt.PyJWKClient:
    # PyJWKClient caches the fetched keys and handles Cloudflare's key rotation.
    return jwt.PyJWKClient(f"https://{team_domain}/cdn-cgi/access/certs", cache_keys=True)


def _signing_key(token: str):
    """Resolve the public key for this token from the team's JWKS (patched in tests)."""
    return _jwks_client(settings.CF_ACCESS_TEAM_DOMAIN).get_signing_key_from_jwt(token).key


class CloudflareAccessMiddleware:
    """Validate the Access JWT on organizer paths and auto-login the Django user."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(PROTECTED_PREFIX):
            if settings.CF_ACCESS_TEAM_DOMAIN and settings.CF_ACCESS_AUD:
                denied = self._authenticate(request)
                if denied is not None:
                    return denied
            elif not settings.DEBUG:
                # Django's own admin login still applies, but the edge gate is missing —
                # say so loudly instead of failing silently open at the edge.
                logger.warning(
                    "CF_ACCESS_* unset in production — /admin relies on Django login only"
                )
        return self.get_response(request)

    def _authenticate(self, request):
        """Return a 403 response to short-circuit with, or None when authenticated."""
        token = request.META.get("HTTP_CF_ACCESS_JWT_ASSERTION", "")
        if not token:
            return HttpResponseForbidden("Cloudflare Access JWT missing")
        try:
            claims = jwt.decode(
                token,
                _signing_key(token),
                algorithms=["RS256"],
                audience=settings.CF_ACCESS_AUD,
                issuer=f"https://{settings.CF_ACCESS_TEAM_DOMAIN}",
                options={"require": ["exp", "email"]},
            )
        except (jwt.PyJWTError, OSError) as exc:
            # OSError covers JWKS fetch failures; both reject rather than fail open.
            logger.warning("Rejected Cloudflare Access JWT: %s", exc)
            return HttpResponseForbidden("Cloudflare Access JWT invalid")

        email = (claims.get("email") or "").strip().lower()
        if not email:
            return HttpResponseForbidden("Cloudflare Access JWT has no email")

        if not (request.user.is_authenticated and request.user.username == email):
            backend = "django.contrib.auth.backends.ModelBackend"
            login(request, self._organizer(email), backend=backend)
        return None

    @staticmethod
    def _organizer(email: str):
        """Get-or-create the Django user for an Access-verified organizer email.

        Passing the Access allow-list *is* the trust decision (§8: organizers are few
        and fully trusted), so organizers get full admin rights.
        """
        user_model = get_user_model()
        user, _ = user_model.objects.get_or_create(
            username=email,
            defaults={"email": email, "is_staff": True, "is_superuser": True},
        )
        if not (user.is_staff and user.is_superuser):
            user.is_staff = True
            user.is_superuser = True
            user.save(update_fields=["is_staff", "is_superuser"])
        return user
