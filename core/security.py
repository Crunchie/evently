"""Security headers + log hygiene (§8 items 2 & 3).

CSP: everything the app renders runs with a strict policy — no inline scripts or
styles exist in our templates (all behaviour lives in static/core/app.js, hung off
data attributes). Django's own admin is the one exception: its widgets still use
scattered inline *styles*, so admin-namespaced views get `style-src 'unsafe-inline'`
(scripts stay external there too).

Token redaction: capability URLs are bearer credentials (§8), and Django's request
logger happily writes "Not Found: /i/<token>" for guest-path 4xxs. The logging
filter rewrites any /i/<token> to /i/[token] before a record reaches a handler.
gunicorn's access log stays off (Dockerfile starts it without --access-logfile).
"""

import logging
import re

STRICT_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; manifest-src 'self'; font-src 'self'; "
    # The guest "Getting there" map is a keyless Google-Maps embed (§2.5); its iframe
    # 301s maps.google.com → www.google.com, so both are framed. Nothing else is.
    "frame-src https://maps.google.com https://www.google.com; "
    "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
DJANGO_ADMIN_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)


class SecurityHeadersMiddleware:
    """Attach the Content-Security-Policy to every response (§8 item 2)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if "Content-Security-Policy" not in response:
            match = getattr(request, "resolver_match", None)
            is_django_admin = bool(match and match.app_name == "admin")
            response["Content-Security-Policy"] = (
                DJANGO_ADMIN_CSP if is_django_admin else STRICT_CSP
            )
        return response


TOKEN_PATH_RE = re.compile(r"/i/[A-Za-z0-9_-]{8,}")


class RedactTokenFilter(logging.Filter):
    """Never log full capability tokens (§8 item 3). Attached to django.request."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = TOKEN_PATH_RE.sub("/i/[token]", message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True
