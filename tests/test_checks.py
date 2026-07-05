"""System check for the 'Freemail in Reply-To, but not From' deliverability trap (§6)."""

import pytest

from core.checks import freemail_reply_to_check


def ids(settings, email_from, reply_to):
    settings.EMAIL_FROM = email_from
    settings.EMAIL_REPLY_TO = reply_to
    return [w.id for w in freemail_reply_to_check(None)]


def test_freemail_reply_to_with_custom_from_warns(settings):
    # The exact mail-tester shape: branded domain From, freemail Reply-To.
    assert ids(settings, "Sam & Kate <invites@sams.party>", "sam@gmail.com") == ["core.W001"]


def test_display_name_from_is_parsed(settings):
    # From carries a display name; the domain must still be extracted from the address.
    assert ids(settings, "Evently <invites@sams.party>", "hosts@outlook.com") == ["core.W001"]


def test_same_domain_reply_to_is_clean(settings):
    assert ids(settings, "invites@sams.party", "replies@sams.party") == []


def test_freemail_both_sides_is_clean(settings):
    # Not the flagged pattern — no custom-domain From to lend borrowed trust.
    assert ids(settings, "sam@gmail.com", "sam@gmail.com") == []


@pytest.mark.parametrize("email_from,reply_to", [("", "sam@gmail.com"), ("invites@sams.party", "")])
def test_unconfigured_is_clean(settings, email_from, reply_to):
    assert ids(settings, email_from, reply_to) == []
