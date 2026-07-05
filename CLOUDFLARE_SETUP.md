# Cloudflare Setup — evently

Everything to configure on the Cloudflare side, in order. Follow it top to bottom once;
afterwards it's the reference for what was done. Dashboard labels drift over time — the
*concepts* here are stable even if a menu is renamed.

**The target architecture** (design doc §8/§9): the app runs on Proxmox in Docker with
**no published ports**. A `cloudflared` container makes an *outbound* connection to
Cloudflare (the Tunnel) — that is the only way traffic reaches the app. Guests hit the
public RSVP pages; the organizer admin is gated at the edge by Cloudflare Access; a WAF
rule rate-limits the guest endpoints. Email DNS (Resend) lives in the same zone.

```
guests ──► https://<HOST>/i/<token> ──┐
                                      ├─► Cloudflare edge ──tunnel──► app:8000
you ────► https://<HOST>/admin ──►[Access]┘        ▲
                                                   └── WAF rate-limit on /i/*
```

**Decisions this doc assumes**
- One public hostname for everything, e.g. `events.<yourdomain>` — written as `<HOST>`
  below. (Guest links look like `https://<HOST>/i/abc123…`.)
- **Every organizer page lives under `/admin`** (Django admin now; the Phase-3/6
  dashboard + send queue will be mounted under `/admin/…` too), so a single Access path
  rule covers the whole organizer side, forever.
- Public, *not* behind Access: `/i/*` (RSVP pages), `/webhooks/resend` (Phase 4 bounce
  webhook — Resend must be able to POST to it), `/healthz`, `/static/*`.

---

## 0. Prerequisites

- A Cloudflare account (free plan is enough for everything below).
- A domain (see step 1 — the only money in the whole stack, ~$10/yr).
- The Proxmox VM with Docker; the repo's `docker-compose.yml` already contains the
  `cloudflared` service — you only need the token from step 2.

## 1. Domain + DNS zone

Either path ends the same way: the domain's nameservers point at Cloudflare.

**Option A — buy via Cloudflare Registrar (recommended, at-cost):**
1. Dashboard → **Domain Registration** → **Register domain** → buy it.
   Nameservers, DNS zone, WHOIS privacy, DNSSEC: all automatic. Done.

**Option B — bought elsewhere (Porkbun, Namecheap, …):**
1. Dashboard → **Add a domain** → enter it → pick the **Free** plan.
2. Cloudflare shows two nameservers (e.g. `ada.ns.cloudflare.com`). Set them at your
   registrar, replacing the registrar's own.
3. Wait for the zone to show **Active** (minutes to hours).

**Zone settings worth flipping now** (domain → **SSL/TLS** and **Rules**):
- SSL/TLS encryption mode: **Full (strict)** (harmless with a tunnel; correct if you
  ever add non-tunnel origins).
- **Edge Certificates → Always Use HTTPS: On**.
- Optional: **HSTS** on (§8 mentions it). Only enable after HTTPS works — it's sticky.

## 2. Cloudflare Tunnel (the only ingress)

Zero Trust dashboard ([one.dash.cloudflare.com](https://one.dash.cloudflare.com)) →
**Networks → Tunnels**:

1. **Create a tunnel** → connector type **Cloudflared** → name it `evently`.
2. On the connector install page, pick **Docker**. Don't run their command — just copy
   the long token out of it (`--token eyJ…`). That is **`TUNNEL_TOKEN`** for `.env`.
   Treat it like a password.
3. **Public Hostname** tab → **Add a public hostname**:
   - Subdomain: `events` (or your choice) · Domain: `<yourdomain>` → this is `<HOST>`
   - Service: **HTTP** · URL: **`app:8000`**
     (`app` is the compose service name — cloudflared resolves it on the compose
     network. This is why the app needs no published ports.)
4. That's it. When `docker compose up` runs on the VM, the tunnel shows **HEALTHY** and
   `https://<HOST>` serves the app. Cloudflare auto-creates the DNS record for the
   hostname (a CNAME to `<tunnel-id>.cfargotunnel.com`) — don't create one manually.

> The dashboard offers Access-style extras per hostname (no TLS verify etc.) — defaults
> are fine for HTTP to a compose-internal service.

## 3. Cloudflare Access (organizer login — Phase 2)

Still in Zero Trust. Free plan covers 50 users; you need 2.

**3a. Team domain** — Zero Trust → **Settings → Custom Pages** (shown at setup as your
team name): you picked `<team>` when first entering Zero Trust, giving
`<team>.cloudflareaccess.com`. That whole string is **`CF_ACCESS_TEAM_DOMAIN`** for `.env`.

**3b. Login method** — Settings → **Authentication → Login methods**:
- **One-time PIN** is on by default — keep it. This is how your wife logs in with zero
  setup: enters her email, gets a 6-digit code. Nothing else required.
- Optional: add **Google** as an identity provider for one-click login (needs a Google
  OAuth client — nice-to-have, not required; OTP alone is fine).

**3c. The application** — **Access → Applications → Add an application → Self-hosted**:
- Name: `evently admin`
- Session duration: **1 week** (organizer convenience vs. safety; up to 1 month if that
  nags too much).
- Application domain — **path-scoped, this is the important part**:
  - Domain: `<HOST>` · Path: `admin` (covers `/admin` and everything under it).
  - Do **not** add a bare `<HOST>` entry — that would put guest RSVP pages behind a
    login and break the product.
- Policy: name `organizers`, action **Allow**, include → **Emails**:
  `mcardlesam@gmail.com` + your wife's email. Nothing else.

**3d. Values for the app** (the Phase-2 middleware validates Access's JWT):
- On the application's overview page copy the **Application Audience (AUD) tag** →
  **`CF_ACCESS_AUD`** in `.env`.
- The middleware fetches signing keys from
  `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs` (no secret needed).

> Defence-in-depth reminder (§8): the JWT check only means something because the app is
> reachable *solely* through the tunnel. Never publish ports on the VM.

## 4. WAF rate-limiting rule (guest endpoints)

Back in the normal domain dashboard → your zone → **Security → WAF → Rate limiting
rules** → create (the free plan includes exactly **one** rule — this is it):

- Name: `guest-endpoints`
- If incoming requests match: field **URI Path** · operator **starts with** · value
  **`/i/`**
- Rate: ~**30 requests / 10 seconds** per IP (a real guest taps a handful of times;
  token brute-forcing needs millions).
- Action: **Block**, for the shortest available timeout (10s is plenty; it just has to
  make scanning impractical).

Everything guest-writable lives under `/i/` (RSVP + channel-change requests), so one
rule covers it. Organizer side needs nothing — Access already blocks strangers.
The Phase-4 webhook (`/webhooks/resend`) is outside `/i/` on purpose: Resend's
legitimate bursts must not hit this rule.

> ⚠️ **2026-07-04: the rule is NOT firing.** 150 requests to `/i/junk` at 40-parallel
> all returned 404 from the app — nothing got a 429. The §8 record says a rule was
> set up, so in the dashboard check that: it actually exists on the
> `samandmonevents.party` zone, it's **deployed** (not saved as draft), the field is
> **URI Path · starts with · `/i/`** (not *equals*), and the counting
> characteristics are per-IP. Re-test afterwards with:
> `seq 80 | xargs -P 20 -I{} curl -s -o /dev/null -w "%{http_code}\n" https://samandmonevents.party/i/junk | sort | uniq -c`
> — expect mostly 429 after the first ~30.

## 5. Email DNS for Resend (Phase 4)

In the [Resend dashboard](https://resend.com) → **Domains → Add domain** → enter
`<yourdomain>`. Resend lists 3–4 DNS records; add each in Cloudflare (zone → **DNS →
Records**) exactly as shown — typically:

- **TXT** `resend._domainkey` — the DKIM key (the one that really matters).
- **TXT/MX** on a `send.` subdomain — SPF + bounce handling.
- These are TXT/MX records — no orange-cloud/proxy involved.

Back in Resend, hit **Verify**; wait for all green. Then add a DMARC record manually
(Resend may not require it, inboxes like it):

- **TXT** `_dmarc` → `v=DMARC1; p=none; rua=mailto:mcardlesam@gmail.com`
  (`p=none` = monitor-only; tighten to `quarantine` later if you care.)  
  This was done in cloudfare DNS settings, add a TXT record

Also in Resend: create an **API key** → **`RESEND_API_KEY`** in `.env`. In Phase 4
you'll also add a **Webhook** (Resend → Webhooks) pointing at
`https://<HOST>/webhooks/resend`, subscribed to **`email.bounced`, `email.complained`,
and `email.failed`** (the only events the handler acts on), and copy its **signing
secret** into `.env` (variable lands with Phase 4).

## 5a. Reply-To inbox via Cloudflare Email Routing (Phase 4)

Resend only **sends** — it can't receive a reply. So `EMAIL_REPLY_TO` needs an address
on the sending domain that forwards to a real inbox. Cloudflare **Email Routing** (free,
unlimited forwarding) does the receiving half. Both live in the same zone and don't
collide: Resend's records sit on `send.` and `resend._domainkey`; Email Routing claims
the **root MX** and **root SPF**, which Resend never uses.

> Why bother: a freemail `EMAIL_REPLY_TO` (e.g. `you@gmail.com`) while `EMAIL_FROM` is a
> custom domain trips the spam-filter heuristic **"Freemail in Reply-To, but not From"**.
> A same-domain reply-to clears it.

1. Zone → **Email → Email Routing → Get started**. Accept the **auto-added DNS records**:
   - **MX** at root → `route1/2/3.mx.cloudflare.net`
   - **TXT SPF** at root → `v=spf1 include:_spf.mx.cloudflare.net ~all`
2. **Routing rules → Custom addresses → Create address**:
   `replies@<yourdomain>` → **Send to** → `mcardlesam@gmail.com`.
   Cloudflare emails that inbox a **verify** link — click it.
3. `.env`: `EMAIL_REPLY_TO=replies@<yourdomain>`. Redeploy; test a reply lands in Gmail.

Two things to watch when accepting the auto-added records:

- **Only one `v=spf1` TXT is allowed per name.** The root has no SPF today (Resend's SPF
  is on `send.`), so Email Routing adds cleanly. If it ever flags an existing root SPF,
  merge both `include:`s into one record rather than keeping two.
- Leave Resend's `send.` **MX** and `resend._domainkey` **TXT** untouched — those are the
  sending side. Email Routing only manages the root MX + root SPF and the return path
  Resend uses (`send.`) keeps its own SPF, so outbound deliverability is unaffected.

## 5b. R2 bucket for Litestream backups (Phase 7)

The SQLite database replicates continuously off-box via Litestream (§9 of the design
doc). Cloudflare R2 is the natural bucket: same account, S3-compatible, free tier
(10 GB) is effectively infinite for this database. **The bucket must stay private**
(§8 item 5 — it holds friends' PII).

1. Dashboard → **R2 Object Storage** → enable R2 if first time (needs a payment
   method on file; the free tier itself costs nothing) → **Create bucket** →
   name `evently-backups`, location Automatic. Leave all public access OFF.
2. R2 → **API tokens** (or Account API tokens with the R2 template) → create a token
   scoped to **Object Read & Write** on `evently-backups` only. Copy the
   **Access Key ID**, **Secret Access Key**, and the account's S3 endpoint
   (`https://<ACCOUNT_ID>.r2.cloudflarestorage.com`).
3. `.env`: fill `LITESTREAM_ENDPOINT`, `LITESTREAM_ACCESS_KEY_ID`,
   `LITESTREAM_SECRET_ACCESS_KEY`.
4. `litestream.yml`: uncomment the `s3:` replica block.
5. `docker compose up -d --build`, then confirm the litestream container logs show
   `replica sync ... replica=s3` and objects appear in the bucket.
6. **Restore drill against R2** (the file-replica drill passed 2026-07-04):
   `docker compose run --rm litestream restore -o /data/restore-drill.db -replica s3 /data/evently.db`
   then integrity-check and delete the file.

## 6. What ends up in `.env`

| Variable | From |
|---|---|
| `ALLOWED_HOSTS` | `<HOST>` (e.g. `events.yourdomain.com`) |
| `TUNNEL_TOKEN` | step 2.2 (tunnel connector token) |
| `CF_ACCESS_TEAM_DOMAIN` | step 3a (`<team>.cloudflareaccess.com`) |
| `CF_ACCESS_AUD` | step 3d (application AUD tag) |
| `RESEND_API_KEY` | step 5 (Resend dashboard) |
| `EMAIL_FROM` | your pick, e.g. `Sam & Kate <invites@yourdomain.com>` |
| `EMAIL_REPLY_TO` | an address **on the `EMAIL_FROM` domain** (e.g. `replies@yourdomain.com`) forwarded to your personal inbox via Email Routing (§5a) — a freemail reply-to (gmail/outlook/…) while From is a custom domain trips spam filters ("Freemail in Reply-To, but not From") |

## 7. Verification checklist

After `docker compose up -d --build` on the VM (**always `--build`** — a plain
restart re-runs the old image; that's what caused the 2026-07-04 "admin 502 /
ungated" confusion):

- [x] Zero Trust → Tunnels shows `evently` **HEALTHY**. *(verified 2026-07-04)*
- [x] `https://<HOST>/healthz` → `{"status": "ok"}` (public, no login). *(verified)*
- [x] `https://<HOST>/admin` → Cloudflare Access login page appears **before** any
      Django page. *(verified: 302 → `samandmon.cloudflareaccess.com` login)*
- [x] App-layer check: a request to `/admin` **without** the Access JWT → **403**
      from the middleware, and the JWKS endpoint is fetchable from the container.
      *(verified in-container 2026-07-04)*
- [ ] One-time PIN with an allow-listed email → gets through; a non-listed email →
      denied at the edge.
- [ ] From the LAN: `curl http://<vm-ip>:8000/healthz` **fails** (no published ports —
      required for the Access trust model, §8).
- [ ] Hammer `https://<HOST>/i/junk` (parallel — sequential curl is too slow to trip
      30/10s: `seq 80 | xargs -P 20 -I{} curl -s -o /dev/null -w "%{http_code}\n"
      https://<HOST>/i/junk`) → turns into **429**s.
      **✗ FAILED 2026-07-04** — 150 hits, zero 429s; see the warning in §4.
- [ ] (Phase 4) Resend domain shows **Verified**; a test email lands from
      `invites@<yourdomain>` with your Reply-To.
- [ ] (Phase 4) Resend **webhook** created → `https://<HOST>/webhooks/resend`, signing
      secret in `.env` as `RESEND_WEBHOOK_SECRET` → `docker compose up -d`. **Currently
      unset** — bounces are rejected (fail-closed) until this is done.

## 8. Record of what was actually configured (fill in as you go)

| Item | Value |
|---|---|
| Domain | `samandmonevents.party` (bought 2026-07-04) |
| Registrar | cloudfare |
| `<HOST>` (public hostname) | `samandmonevents.party` (apex — update `.env` `ALLOWED_HOSTS` if you pick a subdomain instead) |
| Tunnel name / ID | `evently` / `01e938d8-a007-4541-8dfe-0875b29f28ee` |
| Zero Trust team domain | `samandmon.cloudflareaccess.com` |
| Access application + AUD tag | `evently admin` / `6d9323ca7011b68e18544eec23279859af9e13e2619ded1520c46f5ad42500ba` |
| Allow-listed emails | |
| Access session duration | |
| Rate-limit rule (path/rate/action) | `/i/` / 30 per 10s / block |
| Resend domain verified on | (API key in `.env` is send-only restricted — status not queryable; confirm in dashboard) |
| Resend webhook + secret | **not yet configured** — see §7 last item |
| Email From / Reply-To | `Sam & Mon <invites@samandmonevents.party>` / personal inbox |
| DMARC policy | `p=none` |
