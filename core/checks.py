"""Deploy-time system checks for deliverability config (§6).

Registered in CoreConfig.ready(), so `manage.py check` (run on every deploy) surfaces
misconfigurations before they cost inbox placement.
"""

from email.utils import parseaddr

from django.conf import settings
from django.core.checks import Warning, register

# Free webmail providers that spam filters (mail-tester et al.) treat as "freemail".
# A Reply-To on one of these while From is a custom domain looks like phishing —
# a trustworthy-looking sender diverting replies to a disposable inbox.
FREEMAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.co.uk",
        "ymail.com",
        "hotmail.com",
        "hotmail.co.uk",
        "outlook.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "gmx.com",
        "proton.me",
        "protonmail.com",
    }
)


def _domain(address: str) -> str:
    """Lowercased domain of an RFC 5322 address, tolerating a `Name <addr>` display form."""
    addr = parseaddr(address)[1]
    _, _, domain = addr.rpartition("@")
    return domain.lower()


@register()
def freemail_reply_to_check(app_configs, **kwargs):
    """Warn when Reply-To is freemail but From is a custom domain (§6).

    This is the exact shape mail-tester flags as "Freemail in Reply-To, but not From".
    Fix by using a Reply-To on the sending domain (forward it to your personal inbox).
    """
    from_domain = _domain(settings.EMAIL_FROM)
    reply_domain = _domain(settings.EMAIL_REPLY_TO)

    if not from_domain or not reply_domain:
        return []  # unconfigured (e.g. local dev) — nothing to judge
    if reply_domain not in FREEMAIL_DOMAINS or from_domain in FREEMAIL_DOMAINS:
        return []

    return [
        Warning(
            f"EMAIL_REPLY_TO is a freemail address ({reply_domain}) while EMAIL_FROM is "
            f"on a custom domain ({from_domain}).",
            hint=(
                "Spam filters flag this as 'Freemail in Reply-To, but not From' and dock "
                "your score. Set EMAIL_REPLY_TO to an address on the EMAIL_FROM domain "
                f"(e.g. replies@{from_domain}) and forward it to your personal inbox."
            ),
            id="core.W001",
        )
    ]
