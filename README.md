# evently

Self-hosted event invites & RSVP tracking — a private replacement for Facebook Events.
Create an event once, send personal invites over whatever channel each friend actually
uses (Messenger, WhatsApp, email), and collect every RSVP in one place.

> **Status: pre-build (design complete).** No application code yet — the data model is
> drafted in [`core/models.py`](core/models.py) and the build begins at Phase 0 of the
> plan. Start with the docs below.

## Docs

- **[Design Summary](<Events App — Design Summary.md>)** — what it is, the full functional
  spec, and every design decision with its rationale.
- **[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)** — phased build order with a
  verification gate per phase, tooling, and deployment.
- **[mockups/](mockups/)** — self-contained HTML mockups of the guest RSVP pages and the
  organizer dashboard (open in a browser).

## What it does

- Create / edit / cancel events; invite individual contacts or whole **households** with
  one link — every member still counted.
- Reach each guest on their own channel: **email** (automated) or **Messenger / WhatsApp**
  (assisted one-tap share). Every invite is a unique link back to one RSVP page.
- Guests RSVP **Going / Maybe / Can't** with no account, add a note and plus-ones, and add
  the event to their calendar.
- Live dashboard: who's coming, per-guest status, notes, reminders, and nudges.

See the Design Summary for the non-goals and the complete specification.

## Stack

- **Django** (server-rendered) + **HTMX** + one hand-written stylesheet — no JS build step.
- **SQLite** (WAL) with **Litestream** continuous backups.
- **[uv](https://docs.astral.sh/uv/)** for dependency management (Python 3.12+).
- **Resend** for transactional email.
- Self-hosted on Proxmox, exposed via **Cloudflare Tunnel**; organizer login via
  **Cloudflare Access**.

## Development (uv)

> The Django project is created in **Phase 0** of the implementation plan; until then the
> commands below are the intended workflow.

Dependencies are managed with [uv](https://docs.astral.sh/uv/):

```bash
uv sync                                  # create .venv from the lockfile
uv run python manage.py migrate
uv run python manage.py runserver        # http://localhost:8000
uv run python manage.py createsuperuser  # a local organizer account
uv run pytest                            # tests
```

## Deployment (Docker)

One Compose stack — `app` (gunicorn + WhiteNoise) + `cloudflared` (tunnel) + `litestream`
(backups):

```bash
cp .env.example .env      # set SECRET_KEY, TUNNEL_TOKEN, RESEND_API_KEY, …
docker compose up --build
```

The app publishes no host ports — the Cloudflare Tunnel is the only ingress. Full detail
in [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md).

## License

[MIT](LICENSE) © 2026 Sam McArdle
