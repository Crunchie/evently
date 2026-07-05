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
- **CI:** GitHub Actions (`.github/workflows/ci.yml`) — `uv sync --frozen` → ruff check +
  format → pytest, on every push to `main` and every PR.

### Everyday commands
```bash
uv sync                                   # create/update .venv from lockfile
uv run python manage.py runserver         # local dev server
uv run python manage.py makemigrations
uv run python manage.py migrate
uv run python manage.py createsuperuser   # local organizer account (dev)
uv run pytest                             # tests
uv run ruff check . && uv run ruff format # lint/format
docker compose up -d --build              # deploy: ALWAYS --build (plain restart runs the old image)
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
│   ├── management/commands/ # maintenance/one-off commands (no cron jobs — sends are sync)
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
  constraints, delivery state transitions (incl. the bounce webhook, valid + forged
  signature), Access-JWT middleware (valid/invalid/missing), and channel-change approval
  flow.

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

### Phase 2 — Organizer auth (Cloudflare Access) ✅
`core/auth.py`: `CloudflareAccessMiddleware` validates the `Cf-Access-Jwt-Assertion` JWT
(RS256 against the team JWKS via cached `PyJWKClient`; audience + issuer + exp + email
required), then get-or-creates the Django user by verified email with full organizer
rights and auto-logs-in — Access is the only login. Gated paths: `/admin…` only; guest
paths untouched. When `CF_ACCESS_*` unset: inert (normal Django login), with a loud
warning if that happens in production. Behind-proxy settings were already in place
(Phase 0). Edge-side config documented in `CLOUDFLARE_SETUP.md` §3.
- **Gate — passing:** 9 middleware tests (locally-signed RSA tokens, JWKS patched out):
  valid JWT → admin 200 + organizer user created; missing / bad-signature /
  wrong-audience / expired / no-email → 403; guest paths not gated; unconfigured →
  Django login redirect; existing user promoted to organizer. 21 tests green total.

### Phase 3 — Event flow + RSVP page (the core loop, no messaging) ✅
Took the "lean on admin" route: event/guest CRUD stays in Django admin; built the guest page
+ dashboard. **Guest RSVP page** at `/i/<token>` (bespoke CSS per mockups, system fonts,
zero JS — radios + PRG): all states — fresh / already-responded / cancelled (banner,
POST 403) / revoked (410 dead-end, no details leaked) / past (read-only); single 3-button
and household per-member forms; plus-ones (cap-clamped, toggle-aware); shared note;
who's-coming first names behind the toggle; add-to-calendar (`core/ics.py`: escaped
VEVENT with stable UID + Google link). First GET sets `opened_at` + OPENED; submits go
through `advance_state`/append `RsvpEvent(actor=guest)`. `Referrer-Policy: no-referrer`
on all guest responses. **Dashboard** at `/admin/events/<pk>/dashboard/` (staff-only,
linked from admin): headcount stats incl. Total expected, per-invitation table with
per-member statuses + copyable RSVP links (the hand-delivery flow).
- **Gate — passing:** admin-created event → open link → RSVP single + household → counts
  update on the dashboard; no email involved. 15 new tests (36 total green).

### Phase 4 — Dispatcher + email (Resend) + notifications ✅
`core/channels.py` (dispatcher: address resolution incl. household multi-recipient with
dedupe; synchronous `dispatch_email` via Resend's batch endpoint, chunked at 100;
Delivery rows as audit, FAILED kept on provider errors) + `core/messaging.py` (all four
templates: invite / nudge / update / cancellation — text + minimal HTML, every message
carries the RSVP link). **Send & notify screen** at `/admin/events/<pk>/send/` (§2.3
review breakdown: with-email / no-email / retryable, plus nudge / update / cancel+notify
actions; first send flips draft→active; results land on the dashboard banner as ✓/✗).
**Bounce webhook** `POST /webhooks/resend`: Svix-scheme HMAC verification (timestamp
tolerance, constant-time compare, fail-closed on unset secret) → delivery BOUNCED +
invitation through the ladder (an open can't be regressed). Env: `RESEND_WEBHOOK_SECRET`.
- **Gate — passing in test mode:** 14 new tests (50 green) — send flow end-to-end incl.
  idempotency, provider-failure → FAILED + retryable, household dedupe/same-link, nudge
  targets only non-responders, cancel freezes + notifies, forged/stale/missing-signature
  webhooks rejected, valid bounce flips state without regressing opens. **Remaining ops
  (needs the real domain):** verify domain in Resend, send a real invite to yourself,
  configure the webhook + secret (CLOUDFLARE_SETUP.md §5).
- **Ops status (2026-07-04):** domain + tunnel + Access live and verified end-to-end
  (healthz 200 through the tunnel; `/admin` → Access login at the edge; JWT-less request
  to the app → 403; JWKS reachable from the container). Resend API key in `.env`
  (send-only restricted key — good). **Still to do:** create the Resend webhook →
  `RESEND_WEBHOOK_SECRET` in `.env`, and send one real test invite. ⚠️ Deploy lesson:
  the VM ran a **stale image** for half a day (built pre-Phase-2 → admin ungated at the
  app layer) — deploys must be `docker compose up -d --build`, never plain `up -d` or
  `restart`, and tests are now immune to the production `.env` via
  `config/settings_test.py`.

### Phase 5 — Assisted channels + send queue ✅
`core/channels.py` grew **routing** (§2.2: a person goes out on their preferred active
channel; else email > WhatsApp > Messenger; SMS/Telegram never route — no transport yet)
plus `assisted_channels`/`wa_link` (phonenumbers → E.164, `PHONE_REGION` env, default NZ).
**Send queue** at `/admin/events/<pk>/queue/?kind=invite|nudge|update|cancellation|reminder`
— one card per (envelope, assisted channel): WhatsApp deep link / `navigator.share` for
Messenger / copy fallback, each marking an optimistic SHARED Delivery + advancing the
ladder; skip steps over. A household with two WhatsApp parents stays queued until *each*
copy went out (per-channel SHARED pairs). Send review screen now shows the email/assisted/
no-channel three-way split with queue links per action.
- **Gate — code+tests passing** (12 new): routing preferences, wa.me normalisation, queue
  walk/skip/done, two-parent household same-link, mixed household split, nudge queue
  targeting, staff-gating. **Remaining:** walk it once on a real phone (share sheet +
  wa.me hand-off are browser-level, untestable from pytest).

### Phase 6 — Dashboard, reminders, approvals, overrides ✅
Dashboard now has: channel-change **approval queue** (approve = ACTIVE + preferred swap,
reject = delete; guest requests via a new form on the RSVP page → `PROPOSED` channel,
validated: email syntax / phone → E.164 / Messenger addressless; newer request replaces
older), **organizer RSVP override** per envelope (all members in one action, can reset to
no-reply, history `actor=organizer` + `actor_user`, guest can still overwrite later),
row actions (**resend / nudge-one / new link / uninvite**), per-guest routing + last-
contacted timestamps (§2.4 anti-spam), **notes stream**, **response history** (last 30),
and a **day-before reminder** prompt (≤48 h out) driving a new `reminder` message kind
targeting Going/Maybe. Fixed latent template bug: `household.name|default:contact.name`
raises on household rows (Django resolves filter args eagerly) → `Invitation.display_name`.
- **Gate — code+tests passing** (14 new): override actors/history/no-reply reset, revoke →
  410 no-leak, token rotation kills old link, single-guest resend/nudge isolation, request
  validation + member targeting + replacement, approve/reject flow + preferred swap,
  reminder targeting, dashboard streams render. **Remaining:** one full organizer
  workflow on the live instance (invite → chase → approve → override → remind).

### Phase 7 — PWA + security pass + production deploy 🔨 (code ✅, two edge items open)
**Security pass (§8) — done:** strict CSP on every response via
`core/security.py` middleware (`script-src 'self'`, no inline anything; Django's own
admin gets `style-src 'unsafe-inline'` only, its widgets still inline styles). All
inline JS moved to `static/core/app.js` (data-attribute driven, pages still work
JS-free). HSTS (1 yr, prod only). `RedactTokenFilter` on `django.request` rewrites
`/i/<token>` → `/i/[token]` before any log handler (gunicorn access log stays off).
Guest text was already autoescaped (audited: no `|safe` / `autoescape off` /
`style=` in templates). **PWA (§7) — done:** manifest + generated balloon icons
(192/512/maskable + apple-touch 180), minimal service worker (cache-first for
hashed `/static/` only, navigations always network) served at `/admin/sw.js` so its
scope covers the organizer side; wired via `org_base.html` — organizer pages only,
guest pages stay plain. **Litestream — drilled:** restore from the file replica
verified 2026-07-04 (`integrity_check: ok`, all tables). R2 replica wired through
env (`LITESTREAM_*` in compose + `.env.example`), uncomment the s3 block in
`litestream.yml` once the bucket exists (CLOUDFLARE_SETUP.md §9).
- **Gate — remaining:**
  - [ ] ⚠️ **Rate-limit rule does NOT fire** — hammered `/i/junk` 150× at
        40-parallel on 2026-07-04: all 404, zero 429. Fix in the dashboard
        (CLOUDFLARE_SETUP.md §4 — check the rule exists, is **deployed** not
        draft, matches *starts_with* `/i/`, on the right zone), then re-run the
        hammer loop.
  - [ ] Create the private R2 bucket + token, fill `LITESTREAM_*`, uncomment the
        s3 replica, re-drill restore against R2 (§9).
  - [ ] Invite a **real** gathering end-to-end from the phone; co-host OTP login.

### Phase 8 — Polls ✅ (2026-07-05)
Design + decisions in §2.7 of the design summary. `Poll`/`PollOption`/`PollVote`
(migration 0003): one ballot per envelope, per-poll single/multi toggle, results
(counts + names) visible to guests, guest-added options live immediately with
dedupe + caps (100 chars, 20 options). Dashboard: create form + results +
close/reopen/delete/remove-option; guest voting on the RSVP page via
`/i/<token>/poll/<pk>`; admin registration as CRUD backup. `tests/test_polls.py`.

### Phase 9 — Later / maybe (design-doc Phase 2) ⬜
Automated channels as new spokes (Telegram, then SMS); recurring events
(`RECURRENCE-ID`); optional event **itinerary/"The plan"** field (from the mockup). Build
only on demand.

---

## Open decisions still needed
- [x] **Domain name** — `samandmonevents.party`, bought 2026-07-04 via Cloudflare
      Registrar (see CLOUDFLARE_SETUP.md §8).
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

---

## What's left (single source of truth, 2026-07-05)

All application code through Phase 7 is **built, tested (95 green), and deployed** to
the VM. Everything below is either a dashboard/phone action only the organizer can do,
or on-demand future work. Ordered: do the ⚠️ items before inviting anyone real.

### Cloudflare dashboard
- [ ] ⚠️ **Fix the WAF rate-limit rule** — verified NOT firing (150 hits on `/i/junk`,
      zero 429s). Check it exists on the zone, is *deployed* (not draft), and matches
      *URI Path starts with `/i/`*. Re-test per CLOUDFLARE_SETUP.md §4.
- [ ] **R2 bucket for backups** — create private `evently-backups` + scoped token,
      fill `LITESTREAM_*` in `.env`, uncomment the s3 block in `litestream.yml`,
      `docker compose up -d --build`, then re-run the restore drill against R2
      (steps: CLOUDFLARE_SETUP.md §5b; the file-replica drill already passed).
- [ ] Fill the two blank rows in the CLOUDFLARE_SETUP.md §8 record table
      (allow-listed emails; Access session duration).

### Resend dashboard
- [ ] ⚠️ **Create the delivery webhook** → `https://samandmonevents.party/webhooks/resend`,
      subscribed to **`email.bounced`, `email.complained`, `email.failed`** (all the
      handler acts on; other events are ignored). Copy its signing secret into `.env` as
      `RESEND_WEBHOOK_SECRET`, redeploy. Until then these are rejected fail-closed
      (invisible, not broken).
- [ ] Confirm the domain shows **Verified** (the app's API key is send-only, so this
      can't be checked from here), then **send yourself a real test invite** from the
      send screen and check DKIM/SPF pass headers in Gmail (`Show original`).

### Hands-on verification (phone + a second human)
- [ ] Log in to `https://samandmonevents.party/admin` via Access one-time PIN;
      have the co-host do the same; confirm a non-listed email is denied.
- [ ] From the LAN: `curl http://<vm-ip>:8000/healthz` must FAIL (no published
      ports — required for the Access-JWT trust model, §8).
- [ ] On a phone: install the PWA (Add to Home Screen), walk the send queue —
      Messenger share sheet + WhatsApp deep link — and confirm states advance
      shared → opened → responded.
- [ ] **The real gate:** run one genuine gathering end-to-end (invite, chase,
      approve a channel change, override an RSVP, day-before reminder).

### VM housekeeping
- [ ] Check NTP is enabled (`timedatectl`) — the clock jumped hours on 2026-07-04,
      which can break Access-JWT validation (`iat`/`exp`) and confused docker logs.
- [ ] Consider disk encryption on the Proxmox volume holding `./data` (§8 item 5).

### Decisions still open (design doc §11)
- [ ] Confirm the §2 defaults: plus-ones on, show-guest-list off, no RSVP cutoff,
      silent uninvite, household RSVP editable by any link-holder, keep/drop
      `birth_year`.

### Build work — none until wanted (Phase 8, on demand)
Automated chat spokes (Telegram, then SMS), recurring events (`RECURRENCE-ID`),
event itinerary field, scheduled auto-reminders (would introduce the first
worker loop). Build only when a real need shows up.
