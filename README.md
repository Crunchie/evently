# evently

Self-hosted event invites & RSVP tracking — a private replacement for Facebook Events.
Create an event once, send personal invites over whatever channel each friend actually
uses (Messenger, WhatsApp, email), and collect every RSVP in one place.

> **Status: built and in use.** The app is deployed (self-hosted behind a Cloudflare
> Tunnel) and running real events. It's built for one household's needs rather than as a
> general-purpose product — read it as a worked example, not a turnkey install.

## Docs

- **[Design Summary](<Events App — Design Summary.md>)** — what it is, the full functional
  spec, and every design decision with its rationale. **§13 records where the spec runs
  ahead of the code** — check it before assuming a §2 feature exists.
- **[mockups/](mockups/)** — self-contained HTML mockups of the guest RSVP pages and the
  organizer dashboard (open in a browser).

## What it does

- **Events** — create, edit, cancel; drafts go active when you add the first guest.
- **Contacts & households** — a household is one envelope: one invitation, one link, one
  message, but every member still counted individually. `/admin/contacts/` adds a whole
  household, its members, and a contact method each in a single submit.
- **Queue-first sending** — nothing is ever sent as a side effect of another action.
  Adding, nudging, reminding, updating and cancelling all write **pending rows into an
  outbox** and stop. Email leaves on one button; assisted (Messenger/WhatsApp) messages
  leave when you share them *and say so*; guests with no usable channel get a `blocked`
  row rather than being silently skipped. Every message is readable — and rewritable —
  before it goes.
- **Guest RSVP page** — Going / Maybe / Can't with no account, plus a note, plus-ones, an
  `.ics` for their calendar, poll votes, a request to switch channel, and a "something's
  broken" report. Each guest gets a unique unguessable link.
- **Polls** — one ballot per envelope, single- or multi-choice; guests can add options.
- **Per-event dashboard** — who's coming, per-guest status, notes, and the outbox.
  Updates on refresh, not live-push.
- **Organizer PWA** — the admin side is installable on a phone.

Organizer pages all live under `/admin`, so a single Cloudflare Access rule gates the
whole organizer side. Guest pages (`/i/*`) are public by design.

See the Design Summary for the non-goals and the complete specification.

## Stack

- **Django 5.1** (server-rendered), plain forms + POST/redirect/GET, one hand-written
  stylesheet and one small vanilla `app.js` — **no JS build step and no HTMX** (it was in
  the original design and never turned out to be needed; §13 item 7).
- **SQLite** (WAL) with **Litestream** continuous backups.
- **[uv](https://docs.astral.sh/uv/)** for dependency management (Python 3.12+).
- **Resend** for transactional email; Messenger/WhatsApp are assisted-share, not APIs.
- **gunicorn + WhiteNoise**, self-hosted on Proxmox in Docker, exposed via **Cloudflare
  Tunnel**; organizer login via **Cloudflare Access** (JWT → Django auto-login).
- **CI:** GitHub Actions — ruff check + format, pytest, and a `pip-audit` dependency scan.

## Development (uv)

```bash
uv sync                                  # create .venv from the lockfile
uv run python manage.py migrate
uv run python manage.py runserver        # http://localhost:8000
uv run python manage.py createsuperuser  # a local organizer account
uv run pytest                            # tests
uv run ruff check . && uv run ruff format .
```

With `CF_ACCESS_TEAM_DOMAIN` / `CF_ACCESS_AUD` unset — the default locally — the
Cloudflare Access middleware is inert and normal Django login applies.

## Deployment (Docker)

One Compose stack — `app` (gunicorn + WhiteNoise) + `cloudflared` (tunnel) + `litestream`
(backups):

```bash
cp .env.example .env      # set SECRET_KEY, TUNNEL_TOKEN, RESEND_API_KEY, …
docker compose up -d --build
```

**Always `--build`** — a plain restart silently re-runs the previous image.

The app publishes no host ports — the Cloudflare Tunnel is the only ingress, which is
what makes the Access-JWT trust model sound. Full detail in the
[Design Summary](<Events App — Design Summary.md>) §9.

## License

[MIT](LICENSE) © 2026 Sam McArdle
