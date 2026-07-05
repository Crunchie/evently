"""Polls (§2.7): organizer creates from the dashboard; guests vote via their link.
One ballot per envelope; per-poll single/multi choice; guest-added options are live
immediately (trusted-guests model) with dedupe + caps."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from core.models import Contact, Event, Household, Invitation, Poll, PollOption, PollVote

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_client(client, django_user_model):
    user = django_user_model.objects.create_superuser("sam", "sam@example.com", "pw-strong-123")
    client.force_login(user)
    return client


@pytest.fixture
def event(db):
    return Event.objects.create(
        title="Summer BBQ",
        starts_at=timezone.now() + timedelta(days=7),
        status=Event.Status.ACTIVE,
        host_display="Sam & Kate",
    )


@pytest.fixture
def invitation(event):
    return Invitation.objects.create(event=event, contact=Contact.objects.create(name="Dave"))


def make_poll(event, question="What should I cook?", options=("Snags", "Steak"), **kwargs):
    poll = Poll.objects.create(event=event, question=question, **kwargs)
    for text in options:
        PollOption.objects.create(poll=poll, text=text)
    return poll


def vote_url(invitation, poll):
    return reverse("rsvp-poll", args=[invitation.token, poll.pk])


# --------------------------------------------------------------------------- #
#  Organizer: create + manage from the dashboard
# --------------------------------------------------------------------------- #
def test_create_poll_from_dashboard(staff_client, event):
    resp = staff_client.post(
        reverse("event-poll-create", args=[event.pk]),
        {
            "question": "Which weekend?",
            "options": "This one\n\n  Next one  \nthis one\nNEXT ONE",  # blanks + dupes drop
            "multi_choice": "1",
            "allow_guest_options": "1",
        },
    )
    assert resp.status_code == 302 and "did=poll_created" in resp["Location"]
    poll = event.polls.get()
    assert poll.multi_choice and poll.allow_guest_options and not poll.is_closed
    assert list(poll.options.values_list("text", flat=True)) == ["This one", "Next one"]
    assert poll.options.filter(added_by__isnull=False).count() == 0


def test_create_poll_requires_question_and_option(staff_client, event):
    for payload in ({"question": "", "options": "A"}, {"question": "Q?", "options": "  \n "}):
        resp = staff_client.post(reverse("event-poll-create", args=[event.pk]), payload)
        assert "did=poll_error" in resp["Location"]
    assert not event.polls.exists()


def test_poll_close_reopen_delete(staff_client, event, invitation):
    poll = make_poll(event)
    PollVote.objects.create(option=poll.options.first(), invitation=invitation)

    staff_client.post(reverse("poll-action", args=[poll.pk]), {"action": "close"})
    poll.refresh_from_db()
    assert poll.is_closed

    staff_client.post(reverse("poll-action", args=[poll.pk]), {"action": "reopen"})
    poll.refresh_from_db()
    assert not poll.is_closed

    staff_client.post(reverse("poll-action", args=[poll.pk]), {"action": "delete"})
    assert not event.polls.exists() and not PollVote.objects.exists()  # votes cascade


def test_remove_option_cascades_its_votes(staff_client, event, invitation):
    poll = make_poll(event)
    snags, steak = poll.options.all()
    PollVote.objects.create(option=snags, invitation=invitation)

    staff_client.post(
        reverse("poll-action", args=[poll.pk]), {"action": "remove_option", "option": snags.pk}
    )
    assert list(poll.options.all()) == [steak]
    assert not PollVote.objects.exists()
    # garbage option id is a 404, not a 500
    resp = staff_client.post(
        reverse("poll-action", args=[poll.pk]), {"action": "remove_option", "option": "abc"}
    )
    assert resp.status_code == 404


def test_poll_endpoints_require_staff(client, event, invitation):
    poll = make_poll(event)
    create = client.post(reverse("event-poll-create", args=[event.pk]), {"question": "Q"})
    action = client.post(reverse("poll-action", args=[poll.pk]), {"action": "close"})
    assert create.status_code == 302 and "/login/" in create["Location"]
    assert action.status_code == 302 and "/login/" in action["Location"]


def test_dashboard_shows_poll_results(staff_client, event, invitation):
    poll = make_poll(event)
    PollVote.objects.create(option=poll.options.first(), invitation=invitation)
    page = staff_client.get(reverse("event-dashboard", args=[event.pk])).content.decode()
    assert "What should I cook?" in page and "Snags" in page and "Dave" in page


# --------------------------------------------------------------------------- #
#  Guest: voting via the capability link
# --------------------------------------------------------------------------- #
def test_poll_renders_on_guest_page(client, event, invitation):
    make_poll(event)
    page = client.get(invitation.rsvp_path).content.decode()
    assert "What should I cook?" in page and "Snags" in page and "Steak" in page
    assert 'type="radio"' in page  # single-choice poll


def test_single_choice_vote_and_change(client, event, invitation):
    poll = make_poll(event)
    snags, steak = poll.options.all()

    resp = client.post(vote_url(invitation, poll), {"option": snags.pk})
    assert resp.status_code == 302
    assert list(invitation.poll_votes.values_list("option", flat=True)) == [snags.pk]

    client.post(vote_url(invitation, poll), {"option": steak.pk})  # change of heart
    assert list(invitation.poll_votes.values_list("option", flat=True)) == [steak.pk]


def test_multi_choice_submitted_form_is_the_whole_truth(client, event, invitation):
    poll = make_poll(event, multi_choice=True)
    snags, steak = poll.options.all()

    client.post(vote_url(invitation, poll), {"option": [snags.pk, steak.pk]})
    assert invitation.poll_votes.count() == 2

    client.post(vote_url(invitation, poll), {"option": [steak.pk]})  # untick snags
    assert list(invitation.poll_votes.values_list("option", flat=True)) == [steak.pk]


def test_garbage_option_ids_are_ignored(client, event, invitation):
    poll = make_poll(event)
    other_poll = make_poll(event, question="Other?", options=("X",))
    foreign = other_poll.options.get()
    client.post(vote_url(invitation, poll), {"option": ["abc", "", foreign.pk]})
    assert not invitation.poll_votes.exists()  # cross-poll and non-numeric ids don't count


def test_guest_adds_own_option_and_is_auto_ticked(client, event, invitation):
    poll = make_poll(event, allow_guest_options=True)
    client.post(vote_url(invitation, poll), {"new_option": "  Pavlova  "})
    added = poll.options.get(text="Pavlova")
    assert added.added_by == invitation
    assert list(invitation.poll_votes.values_list("option", flat=True)) == [added.pk]
    # case-insensitive dedupe: a second "pavlova" reuses the existing option
    other = Invitation.objects.create(event=event, contact=Contact.objects.create(name="Ana"))
    client.post(vote_url(other, poll), {"new_option": "pavlova"})
    assert poll.options.count() == 3 and added.votes.count() == 2


def test_typed_option_wins_on_single_choice(client, event, invitation):
    poll = make_poll(event, allow_guest_options=True)
    snags = poll.options.first()
    client.post(vote_url(invitation, poll), {"option": snags.pk, "new_option": "Pavlova"})
    vote = invitation.poll_votes.get()
    assert vote.option.text == "Pavlova"


def test_guest_options_blocked_when_disallowed_and_capped(client, event, invitation):
    poll = make_poll(event, allow_guest_options=False)
    client.post(vote_url(invitation, poll), {"new_option": "Nope"})
    assert poll.options.count() == 2

    capped = make_poll(event, question="Capped?", options=(), allow_guest_options=True)
    for n in range(20):
        PollOption.objects.create(poll=capped, text=f"Option {n}")
    client.post(vote_url(invitation, capped), {"new_option": "Twenty-first"})
    assert capped.options.count() == 20  # at the ceiling the typed text is dropped


def test_closed_poll_rejects_votes_but_shows_results(client, event, invitation):
    poll = make_poll(event, is_closed=True)
    resp = client.post(vote_url(invitation, poll), {"option": poll.options.first().pk})
    assert resp.status_code == 403 and not invitation.poll_votes.exists()
    page = client.get(invitation.rsvp_path).content.decode()
    assert "This poll is closed." in page and "Snags" in page


def test_past_event_locks_voting(client, invitation, event):
    poll = make_poll(event)
    event.starts_at = timezone.now() - timedelta(days=1)
    event.save(update_fields=["starts_at"])
    resp = client.post(vote_url(invitation, poll), {"option": poll.options.first().pk})
    assert resp.status_code == 403


def test_revoked_envelope_cannot_vote_and_leaves_counts(client, event, invitation):
    poll = make_poll(event)
    snags = poll.options.first()
    hh = Household.objects.create(name="The Hendersons")
    Contact.objects.create(name="Jane", household=hh)
    hh_inv = Invitation.objects.create(event=event, household=hh)
    PollVote.objects.create(option=snags, invitation=hh_inv)
    hh_inv.state = Invitation.State.REVOKED
    hh_inv.save(update_fields=["state"])

    resp = client.post(f"/i/{hh_inv.token}/poll/{poll.pk}", {"option": snags.pk})
    assert resp.status_code == 410  # the soft dead-end, like the RSVP page
    page = client.get(invitation.rsvp_path).content.decode()
    assert "The Hendersons" not in page  # revoked ballot out of names and counts


def test_vote_against_another_events_poll_404s(client, invitation):
    other_event = Event.objects.create(
        title="Other", starts_at=timezone.now() + timedelta(days=3), status=Event.Status.ACTIVE
    )
    foreign_poll = make_poll(other_event)
    resp = client.post(vote_url(invitation, foreign_poll), {"option": ""})
    assert resp.status_code == 404


def test_household_ballot_shows_household_name(client, event):
    poll = make_poll(event)
    hh = Household.objects.create(name="The Hendersons")
    Contact.objects.create(name="Jane", household=hh)
    hh_inv = Invitation.objects.create(event=event, household=hh)
    client.post(vote_url(hh_inv, poll), {"option": poll.options.first().pk})

    other = Invitation.objects.create(event=event, contact=Contact.objects.create(name="Ana"))
    page = client.get(other.rsvp_path).content.decode()
    assert "The Hendersons" in page  # one ballot, named for the envelope
