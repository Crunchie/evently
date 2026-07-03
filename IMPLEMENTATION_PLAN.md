# Events — Implementation Plan

Phased build plan for the events/RSVP app. The **what and why** lives in
`Events App — Design Summary.md` (referenced as §N below); this doc is the **how and in
what order**, with a verification gate per phase. Tooling conventions mirror the sibling
`../keep` project (uv, Docker, `.env`, management CLI, `uv run pytest`) — adapted for
Django (keep is FastAPI) and Cloudflare Tunnel/Access (keep uses Tailscale).

> Status legend: ✅ done · 🔨 in progress · ⬜ not started

---

## Stack & tooling

- **Python 3.12+, managed with [uv](https://docs.astral.sh/uv/)** (matches keep: uv
  0.11, `~/.local/bin`). `uv sync` builds `.venv/` from the lockfile; run everything via
  `uv run …`. `pyproject.toml` sets `[tool.uv] package = false` — this is an application,
  not an installable library.
- **Django 5.1+** (server-rendered) · **SQLite** (WAL) · **HTMX** + one hand-written CSS
  file, no build step (§9) · **gunicorn** + **WhiteNoise** for serving.
- **Deploy:** one Docker Compose stack — `app` + `cloudflared` + `litestream` — self-hosted
  on Proxmox behind a Cloudflare Tunnel (§9). The app publishes **no host ports**; only
  the tunnel reaches it (required for the Access-JWT trust model, §8).
- **Email:** Resend (§6). **Assisted channels:** Messenger (Web Share API) + WhatsApp
  (`wa.me` deep links), no server integration (§6).

### Everyday commands
```bash
uv sync                                   # create/update .venv from lockfile
uv run python manage.py runserver         # local dev server
uv run python manage.py makemigrations
uv run python manage.py migrate
uv run python manage.py createsuperuser   # local organizer account (dev)
uv run pytest                             # tests
uv run ruff check . && uv run ruff format # lint/format
docker compose up --build                 # full stack (app+cloudflared+litestream)
```

## Project layout
```
events/
├── pyproject.toml            # uv deps, package=false, pytest+ruff config
├── uv.lock
├── .env / .env.example       # SECRET_KEY, DATA_DIR, RESEND_API_KEY, CF_ACCESS_*, TUNNEL_TOKEN
├── .gitignore
├── Dockerfile                # single-stage, uv, gunicorn, non-root
├── docker-compose.yml        # app + cloudflared + litestream
├── litestream.yml            # SQLite backup config
├── manage.py
├── config/                   # Django project (settings package)
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py / asgi.py
├── core/                     # the single app
│   ├── models.py             # ✅ already drafted
│   ├── admin.py              # organizer backoffice (§2.6 CRUD)
│   ├── auth.py               # Cloudflare Access JWT → Django user
│   ├── views/               # rsvp page, dashboard, send queue, event CRUD
│   ├── channels/            # dispatcher + email/messenger/whatsapp plugins
│   ├── templates/
│   ├── static/              # app.css (bespoke), htmx.min.js, manifest, sw.js
│   ├── management/commands/ # process_deliveries, send_reminders, …
│   └── migrations/
├── tests/                    # pytest-django
├── data/                     # SQLite db + litestream (gitignored, bind-mounted to /data)
├── mockups/                  # ✅ HTML mockups (rsvp-guest, rsvp-household, dashboard)
└── Events App — Design Summary.md
```

## Configuration (env vars)
```
SECRET_KEY=            # Django secret; gen: python -c "import secrets;print(secrets.token_hex(32))"
DATA_DIR=./data        # SQLite + backups (Docker: /data)
DEBUG=0                # 1 in local dev
ALLOWED_HOSTS=         # e.g. rsvp.sams.party
RESEND_API_KEY=        # Phase 4+
EMAIL_FROM=            # e.g. "Sam & Kate <invites@sams.party>"
EMAIL_REPLY_TO=        # personal inbox (§6)
CF_ACCESS_TEAM_DOMAIN= # e.g. sams.cloudflareaccess.com  (Phase 2+)
CF_ACCESS_AUD=         # Access application AUD tag       (Phase 2+)
TUNNEL_TOKEN=          # cloudflared tunnel token         (deploy)
```
Settings read env via `python-dotenv` at import (keep's pattern). SQLite in WAL:
```python
DATABASES["default"]["OPTIONS"] = {
    "init_command": "PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;",
    "transaction_mode": "IMMEDIATE",
}
```

## Dependencies (`pyproject.toml`)
```toml
[project]
name = "events"
requires-python = ">=3.12"
dependencies = [
    "django>=5.1",
    "gunicorn>=23.0",
    "whitenoise>=6.7",
    "python-dotenv>=1.0",
    "resend>=2.0",          # transactional email (§6)
    "phonenumbers>=8.13",   # normalise phones to E.164 for wa.me links (§6)
    "pyjwt[crypto]>=2.9",   # validate the Cloudflare Access JWT (§8)
    "httpx>=0.27",          # fetch Access JWKS
]
[dependency-groups]
dev = ["pytest>=8", "pytest-django>=4.9", "ruff>=0.6"]

[tool.uv]
package = false

[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "config.settings"
pythonpath = ["."]
testpaths = ["tests"]
```

## Deployment artifacts

**Dockerfile** (mirrors keep; Django variant)
```dockerfile
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 DJANGO_SETTINGS_MODULE=config.settings
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY . .
RUN SECRET_KEY=build uv run python manage.py collectstatic --noinput
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data && chown -R appuser:appuser /srv /data
USER appuser
ENV PATH="/srv/.venv/bin:$PATH" DATA_DIR=/data
EXPOSE 8000
CMD ["sh","-c","python manage.py migrate --noinput && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3"]
```

**docker-compose.yml** — note: `app` exposes **no ports**; the tunnel is the only ingress.
```yaml
services:
  app:
    build: .
    user: "${PUID:-1001}:${PGID:-1001}"
    environment:
      - SECRET_KEY=${SECRET_KEY:?set in .env}
      - DATA_DIR=/data
      - ALLOWED_HOSTS=${ALLOWED_HOSTS}
      - RESEND_API_KEY=${RESEND_API_KEY:-}
      - CF_ACCESS_TEAM_DOMAIN=${CF_ACCESS_TEAM_DOMAIN:-}
      - CF_ACCESS_AUD=${CF_ACCESS_AUD:-}
    volumes:
      - ./data:/data
    restart: unless-stopped
    healthcheck:
      test: ["CMD","python","-c","import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"]
      interval: 30s
      timeout: 5s
      retries: 3
  cloudflared:
    image: cloudflare/cloudflared:latest
    command: tunnel run
    environment: [ "TUNNEL_TOKEN=${TUNNEL_TOKEN:?}" ]   # points hostname → http://app:8000
    depends_on: [app]
    restart: unless-stopped
  litestream:
    image: litestream/litestream:latest
    command: replicate
    volumes:
      - ./data:/data
      - ./litestream.yml:/etc/litestream.yml:ro
    restart: unless-stopped
```

## Testing strategy
- `pytest` + `pytest-django`, run via `uv run pytest` (keep parity). `tests/` holds one
  module per area; each phase's gate ships with the tests that prove it.
- Fast, DB-backed tests (SQLite temp). Key targets: capability-token routing & the RSVP
  state machine, headcount math (individuals + household + plus-ones), the XOR/uniqueness
  constraints, delivery-queue transitions, Access-JWT middleware (valid/invalid/missing),
  and channel-change approval flow.

---

## Implementation phases (with gates)

### Phase 0 — Scaffolding & tooling ✅
`pyproject.toml` + `uv sync`; `config` project + `core` app; env-driven settings;
WhiteNoise; `.env.example`, `.gitignore`, `.dockerignore`, Dockerfile, docker-compose
(app+cloudflared+litestream), `litestream.yml`; `/healthz` view; ruff config.
- **Gate — all passing:** `runserver` serves `/healthz` → `{"status":"ok"}`; `uv run
  pytest` green; `docker compose build` succeeds (518 MB image); ruff check + format clean.
- Note: the initial `core` migration (`0001_initial`) was generated here so the stack is
  fully runnable, and SQLite **WAL is confirmed active**. That covers Phase 1's
  makemigrations — Phase 1 now just adds the admin registration + constraint tests.

### Phase 1 — Data model + admin backoffice ✅
Models + migration + WAL done in Phase 0. Registered everything in the Django admin with
list displays + inlines (attendees inline on invitations; channels inline on contacts;
members inline on households; append-only `RsvpEvent` view-only) — the free organizer CRUD.
- **Gate — passing:** `manage.py check` clean; tests cover the constraints (contact XOR
  household, no double-invite, one preferred channel/contact, one attendee row/person) and
  `expected_headcount` (individuals + household members + envelope plus-ones); admin
  changelists + the invitation add-form (with attendee inline) all render 200. 8 tests green.

### Phase 2 — Organizer auth (Cloudflare Access) ⬜
`core/auth.py`: middleware validating the `Cf-Access-Jwt-Assertion` JWT against the team
JWKS (`CF_ACCESS_TEAM_DOMAIN`/`CF_ACCESS_AUD`), get-or-create the Django user by verified
email, auto-login (§8). Local-dev bypass: when `CF_ACCESS_*` unset (DEBUG), fall back to
normal Django admin login. Behind-proxy settings (`SECURE_PROXY_SSL_HEADER`,
`CSRF_TRUSTED_ORIGINS`, `ALLOWED_HOSTS`).
- **Gate:** request with a forged-but-well-formed dev JWT logs in; missing/invalid → 403
  in prod mode; dev login works locally. Middleware unit-tested.

### Phase 3 — Event flow + RSVP page (the core loop, no messaging) ⬜
The heart of the product, usable by copy-pasting links by hand. Organizer views for
create/edit(+notify choice)/cancel/clone and the guest-list builder (or lean on admin for
v1 and build just the dashboard + RSVP page). **Guest RSVP page** at `/i/<token>`: bespoke
CSS per the mockups; all states — fresh / already-responded / cancelled / revoked / past
(§2.5); Going/Maybe/Can't, note, plus-ones, household per-member; add-to-calendar (`.ics`
`VEVENT` + Google link); who's-coming toggle. Submits write `InvitationAttendee` +
`RsvpEvent`; first hit sets `opened_at`.
- **Gate:** create event (admin) → open an invitation link → RSVP (single + household) →
  see counts update on a basic dashboard. No email involved yet. State machine tested.

### Phase 4 — Dispatcher + email (Resend) + notifications ⬜
`core/channels/` dispatcher interface (automated vs assisted); **email plugin** via Resend
sending from the verified domain with `Reply-To` (§6). `deliveries` rows are the queue;
`manage.py process_deliveries` (cron, every minute) sends queued items and records
sent/bounced. Notification templates: invite, nudge, update, cancellation (§2.4). Send
review screen (§2.3). Ops: register domain + SPF/DKIM/DMARC.
- **Gate:** send a real invite to yourself from your domain; open→responded tracked;
  a bounce flips state and prompts "try another channel"; nudge non-responders works.

### Phase 5 — Assisted channels + send queue ⬜
Messenger via `navigator.share`; WhatsApp via `wa.me/<E.164>?text=` (phones normalised with
`phonenumbers`); the **send-queue** UI (share → next), optimistic "shared" state, desktop
"copy invite" fallback (§6). Delivery tracking; first link-click is the real signal.
- **Gate:** on a phone, walk the send queue for a few contacts across Messenger + WhatsApp;
  links carry the token; states advance shared→opened→responded.

### Phase 6 — Dashboard, reminders, approvals, overrides ⬜
Full dashboard (§2.6, mockup): headcount, per-guest table with household expansion, notes
stream, response history, **pending channel-change approvals** (§2.5), **organizer RSVP
override** (§2.3, `actor=organizer`), day-before reminder prompt (§2.4).
- **Gate:** run a complete organizer workflow on a test event: invite, chase, approve a
  channel change, override an RSVP, send a reminder.

### Phase 7 — PWA + security pass + production deploy ⬜
PWA manifest + service worker (§7). Security pass (§8): CSP, `Referrer-Policy: no-referrer`
on the RSVP page, escape all guest-authored text, rate-limit RSVP + channel-request +
login, don't log tokens. Stand up the Cloudflare Tunnel + Access on the Proxmox VM;
Litestream backups to a private bucket; restore drill.
- **Gate:** invite a **real** gathering end-to-end from the phone; confirm Access gates the
  dashboard and your co-host gets in via one-time PIN; verify a Litestream restore.

### Phase 8 — Later / maybe (design-doc Phase 2) ⬜
Automated channels as new spokes (Telegram, then SMS); recurring events
(`RECURRENCE-ID`); optional event **itinerary/"The plan"** field (from the mockup). Build
only on demand.

---

## Open decisions still needed
- [ ] **Domain name** — blocks Resend DNS (SPF/DKIM/DMARC) and the tunnel hostname (§9).
- [ ] **Confirm §2 defaults** — plus-ones on, show-guest-list off, no RSVP cutoff, silent
      uninvite, cover images deferred, household RSVP editable by any link-holder,
      `birth_year` field for kids kept or dropped.

## Differences from `../keep` (intentional)
- **Django, not FastAPI/SQLModel** — decided in §9 for the free admin + batteries.
- **Cloudflare Tunnel + Access, not Tailscale** — decided in §8/§9; gives a public
  `rsvp.<domain>` for guests (Tailscale is private-only, wrong for public RSVP links).
- **Compose adds `cloudflared` + `litestream`**; app exposes no host ports.
- Same everywhere else: uv workflow, `package = false`, single-stage uv Dockerfile,
  non-root, `./data` bind mount, `.env` pattern, `uv run pytest`.
