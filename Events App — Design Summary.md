# Events App — Design Summary

A small, self-owned service for inviting friends to gatherings and tracking who's
coming — a replacement for Facebook Events that doesn't require everyone to be on one
platform. Create an event once, send personal invites over whatever channel each friend
actually uses (Messenger, WhatsApp, email), and watch the RSVPs land in one place
instead of scattered across DMs, texts, and "did you see my message?"

> Status: early design. This doc captures decisions and rationale from initial scoping.
> Flesh out the `TODO` sections with Claude Code.
>
> **Visual mockups** of the approved UI direction live in `mockups/` (self-contained
> HTML, open in a browser): `rsvp-guest.html` and `rsvp-household.html` (guest RSVP pages
> — sunset-gradient hero + frosted RSVP card, the agreed look), and `dashboard.html`
> (organizer dashboard, older Pico styling, not yet restyled to match).
>
> **Build plan:** `IMPLEMENTATION_PLAN.md` (phased, with gates; uv tooling).
> **Data model:** drafted in `core/models.py`.

---

## 1. Goal

### The problem
Organizing a get-together today means picking a platform and forcing everyone onto it.
Facebook Events only reaches people on Facebook; a group chat buries the details and has
no real RSVP state; a mass email gives you no way to track replies. The invitees who
don't use your chosen tool either get missed or reply through some other channel that
you then have to reconcile by hand. **There is no single source of truth for "who is
actually coming."**

### What this builds
One app that owns the event and the guest list, and meets each guest *where they already
are*. Every invite — regardless of channel — leads back to the same place to respond,
and every response flows back into one canonical RSVP record. You stay in control of
your data, it costs ~$0 to run, and people don't need an account to RSVP.

### How it's used (the core loop)

*As the organizer:*
1. Create an event — title, time, location, description.
2. Build the guest list from your contacts (a contact can have several ways to be
   reached: Messenger, WhatsApp, email).
3. Hit send. The app picks the right channel per person and delivers a personal invite —
   a link to their own RSVP page — over that channel.
4. Watch a live dashboard fill in — Going / Maybe / Can't, per person, in real time.
5. Send a reminder to the people who haven't responded (or to the "Going" crowd the day
   before). Edit event details and everyone gets the update.

*As an invitee:*
1. Receive an invite on a channel you already use — no app to install, no account to make.
2. Tap your unique link.
3. Pick Going / Maybe / Can't, optionally with a note. Done.
4. See the event details and add it to your own calendar.

### Features (summary — full functional spec in §2)

*Organizer-facing* (behind a login — see §8)
- Create / edit / cancel events; edits propagate to everyone already invited.
- Contact list with multiple channels per person; reusable across events.
- Invite whole **households/families** with one link — every member still counted (§2.2).
- One-click send; automatic per-person channel selection via the dispatcher.
- Live RSVP dashboard (counts + per-guest status + response history).
- Reminders and nudges to non-responders.

*Invitee-facing*
- No account required — a single unguessable link per invitation is the whole identity.
- Respond from any channel; one shared RSVP page is the universal fallback.
- Can request a different contact method (Messenger / WhatsApp / email) for updates +
  future invites; the organizer approves and their contact card updates (§2.5).
- Add to calendar (`.ics` download / Google Calendar link) for everyone.

*System*
- One canonical RSVP record per person per event — the single source of truth.
- Pluggable channels (spokes) added one at a time behind a common dispatcher interface.
- Latest response wins, with the full change history kept (§5).

### Non-goals (at least for now)
- Not a public event-discovery / ticketing platform — it's private invites to people
  you already know.
- No social feed, comments, photo sharing, or "X is interested" virality.
- No payments / paid tickets.
- **No native calendar Accept/Decline (ICS REPLY) — permanently out of scope.** Guests
  can *add* an event to their calendar, but responses always come back through the RSVP
  page, never as a calendar reply. This deliberately avoids inbound-email parsing and
  the extra services it needs; the RSVP page already covers responses (§4).
- Recurring events are deferred until the single-event flow is solid (§5) — the
  **clone event** feature (§2) covers the "monthly BBQ" case pragmatically.

### Guiding constraints
- **Self-owned & low cost** — runs on existing homelab infra (§9); target ~$0/month.
- **Channel-agnostic** — the product must work for someone reachable *only* by WhatsApp
  or *only* by email; no channel is mandatory except as a way to deliver one link.
- **No lock-in for invitees** — responding never requires signing up for anything.

## 2. Functional specification

What the app actually does, in detail. This is the contract for Phase 1 unless marked
otherwise. Defaults are chosen for house-gathering scale (~30 guests); judgment-call
defaults are marked **(default)** and easy to flip later.

### 2.1 Events — create & manage

**Fields:** title; start date+time (required); end time (optional); location (free text
+ optional map URL); description (plain text with line breaks in v1); host display name
(defaults to organizer, editable — e.g. "Sam & Kate"). *Cover image: deferred* — adds
upload/storage/resizing complexity for polish that can come later.

**Lifecycle:** `draft → active → cancelled`, with *past* derived from the start time.

- **Draft** — build the guest list, preview the RSVP page exactly as a guest will see
  it, nothing sent yet, everything editable. Events start here.
- **Send** — the first send flips draft → active.
- **Edit while active** — organizer chooses per edit: **"notify guests"** (material
  changes: time, place, cancellation-adjacent stuff) queues an update notification over
  each guest's channel; **silent** (typo fixes) doesn't. Either way the RSVP page always
  shows current truth — the link is *living, not a snapshot* — so even unnotified guests
  are never looking at stale details.
- **Cancel** — confirmation step, then a cancellation notice to everyone invited; the
  RSVP page flips to a "cancelled" state and stops accepting RSVPs.
- **Clone** — copy any event (details + guest list; RSVPs and tokens reset) as a new
  draft. This is the pragmatic answer to recurring gatherings without recurring-event
  machinery.
- **Delete** — drafts only. Past events archive instead (kept as history and as clone
  sources).

**Per-event toggles:**
- **Plus-ones** — **on (default)**, with an optional per-guest cap. Guests state how
  many they're bringing; counts feed the headcount.
- **Show who's coming** — **off (default)** for privacy (§8). When on, the RSVP page
  shows **first names of "Going" guests only** — never contact info, notes, or numbers.

### 2.2 Contacts & the guest list

- **Contacts** are event-independent and reusable: name (+ optional nickname, used in
  greetings), channels (email address, phone number for WhatsApp, and/or a Messenger
  flag — Messenger needs no address since sends are assisted, §6), free-text notes, and
  **tags** ("family", "book club") for bulk operations.
- **Building a guest list:** pick individual contacts, add a whole tag at once, or
  **quick-add** a brand-new contact inline (name + one channel, no full form detour).
- **Per-invitation channel:** defaults to the contact's preferred channel, overridable
  per event ("Dave by email this time").
- **Households / families:** contacts can be grouped into a named **household** ("The
  Hendersons"). Members are ordinary contacts, and members with *no* channel at all
  (kids) are fine — contacts never required channels. A household is invited as one
  unit with one link (§2.3), while every member still counts individually in the
  headcount (§2.6). A contact belongs to at most one household.
- **Channel provenance & approval:** each channel on a contact card records who supplied
  it — organizer-entered or **guest-requested** via the RSVP page (§2.5). Guest requests
  don't touch the card until the organizer approves them (one-tap review, §2.6); on
  approval the new channel becomes the contact's preferred. The organizer can always
  override.
- **Uninvite:** removes the invitation and revokes its token — the guest's link stops
  working (soft "this invitation is no longer available" page). Silent — no notification
  **(default)**. History is retained.
- Contact dedupe/merge: manual editing only in v1.

### 2.3 Invites & delivery management

**Invitation lifecycle:**
`pending → queued → sent / shared → opened → responded`, plus `bounced` for failed email.
The state is a **monotonic ladder — it only moves forward**: a link click after
responding can't regress the envelope to merely "opened", and with several deliveries
(household copies to both parents) the envelope shows the *furthest* progress — a bounce
on one copy only applies while nothing has been opened, and a later open clears it.
`revoked` (§2.2) always applies and is terminal.

- *sent* = provider accepted the email; *shared* = share sheet / deep link invoked
  (assisted channels — optimistic, §6); *opened* = first click of the unique link
  (**the real delivery signal** for every channel); *responded* = RSVP recorded.
- **Send flow:** a review screen summarizes the plan ("12 by email, 15 by Messenger,
  3 with no channel — fix?"). Confirm → emails go out as a batch; assisted-channel
  invitees enter the **send queue** (share → next → share, §6). Guests with no usable
  channel are flagged, not silently skipped.
- **Household invitations are one envelope:** inviting a household creates a *single*
  invitation with a *single* link covering all members. Delivery can go to more than one
  member's channel (e.g. both parents get the same link), and whoever opens it RSVPs for
  the household (§2.5). One envelope, many attendees — see §5.
- **Universal escape hatch:** the organizer can always **copy any guest's unique link**
  and deliver it by hand over anything — carrier pigeon compatible.
- **Per-guest actions, any time:** resend; switch channel and resend; copy link;
  regenerate token (revokes the old link); uninvite.
- **Set RSVP on a guest's behalf:** the organizer can directly set any attendee's status
  (Going / Maybe / Can't / back to no-reply), plus-ones count, and note — for the friend
  who replied in person or in a group chat and won't touch the link. Works per household
  member, so "the parents are in, the kids aren't" is one action. Recorded in history as
  an **organizer-made** change (§5), and the guest can still override it later via their
  link (last-write-wins) — the manual value is a real answer, not a lock.
- **Email bounces** surface on the dashboard with a "try another channel" prompt.

### 2.4 Reminders & updates

All notifications reuse each guest's invite channel (email = automated send; assisted
channels = the affected guests re-enter the send queue).

- **Nudge non-responders** — one tap; templated message; shows exactly who will receive
  it before confirming.
- **Day-before reminder** to Going/Maybe guests — offered as a prompt per event; manual
  confirm in v1 (a scheduled auto-send toggle is possible later — it would introduce the
  first clock-driven job, via a worker loop, §9).
- **Change notifications** on material edits and **cancellation notices** (§2.1).
- **Anti-spam guard:** per-guest last-contacted timestamps are shown, and duplicate
  nudges won't stack in the queue.

### 2.5 What a guest sees and does (RSVP page)

The page behind their unique link is the *entire* guest-side product. No login, ever (§8).
*Mockups: `mockups/rsvp-guest.html` (single guest) and `mockups/rsvp-household.html`.*

**A guest sees:**
- A personal greeting ("Hi Alex 👋"), host name(s), event title, time, location
  (+ map link), description — always the **current** version on any revisit.
- Their own current RSVP status, if they've already responded.
- *If the event enables it:* first names of who's going (§2.1 toggles).
- A **cancelled** page if the event was cancelled; a soft **"no longer available"** page
  if their invitation was revoked.

**A guest can:**
- **RSVP Going / Maybe / Can't** — one tap. Then optionally add a **note to the host**
  ("we'll be late", "bringing pavlova") and a **plus-ones count** (if enabled).
- **Change their RSVP and note any time up to event start** — same link. Every change is
  recorded in the append-only history (§5); no RSVP cutoff in v1 **(default)**.
- **RSVP for their household** — a household link lists all members; whoever holds the
  link ticks Going / Maybe / Can't per member (kids included) and adds a shared note.
  Any member with the link can update it later **(default)**.
- **Switch how they're reached** — a guest can request a different preferred channel
  for this event's updates *and future invites*, choosing from **all supported
  mechanisms: Messenger, WhatsApp, or email**, entering whatever details it needs
  (email address; phone number for WhatsApp; Messenger needs none). The request sits
  **pending until the organizer approves it** — a one-tap review on the dashboard, so
  you can eyeball that `dave.new@gmail.com` plausibly belongs to Dave before it takes
  effect (§8). On approval the contact card updates and the new channel becomes their
  preferred. Deliberately simple: no automated verification — the organizer's review is
  the gate. Side benefit: guests who switch to email move themselves off the manual send
  queue onto the automated path.
- **Add to calendar** — download a plain `.ics` file (a `VEVENT` — an event to *add*,
  not a reply mechanism) and/or use a Google Calendar quick-add link.

**A guest never sees:** other guests' contact details or notes, delivery states, counts
beyond the opt-in first-name list, or anything about other events. There are no guest
accounts, comments, or photos (§1 non-goals). Known accepted risk: the link is a bearer
capability — a forwarded link can RSVP as that person (§8).

### 2.6 Organizer dashboard (per event)

*Mockup: `mockups/dashboard.html`.*

- **Headcount at the top:** Going / Maybe / Can't / no-reply counts, plus
  **total expected = every attendee marked Going (individuals *and* household members)
  + plus-ones** — the number you actually cater for.
- **Per-guest table:** one row per invitation (a household is one row, expandable to
  per-member statuses); name, channel, invite state with timestamps (sent / shared /
  opened / responded / bounced), RSVP status, plus-ones, latest note. Row actions:
  nudge, resend, copy link, switch channel, **set RSVP status / plus-ones on their
  behalf** (§2.3), uninvite.
- **Pending channel-change requests** (§2.5) queue here for one-tap approve / reject;
  approval updates the contact card and future sends follow it.
- **Notes stream:** all guest notes in one place ("bringing pavlova") — the stuff that
  changes what you buy.
- **Response history** per guest (from `rsvp_events` — who flip-flopped, when, and
  whether each change came from the guest or the organizer).
- **Event actions:** edit (+ notify choice), cancel, clone, toggles (§2.1).
- Everything sits behind the Access-gated admin (§8). v1 leans on the **Django admin**
  for plain CRUD (contacts, tags, event fields — §9); the **dashboard** and the **send
  queue** are the two hand-built organizer views that deserve polish.

### 2.7 Polls

Organizer asks the room a question ("which weekend?", "what should I cook?"); guests
answer on their RSVP page. Decisions settled 2026-07-05:

- **Created and managed from the dashboard** (question + options, one per line);
  close / reopen, delete poll, remove individual options (removing an option deletes
  its votes). Django admin is CRUD backup, per the usual split (§2.6).
- **One ballot per envelope** (invitation), not per attendee: a household's shared
  link casts one set of votes. Polls gauge the room's preference — per-person
  headcounts are what attendee RSVPs are for. Voter names shown are the envelope's
  display name ("The Hendersons").
- **Per-poll single/multi toggle:** radios ("BBQ or picnic?") or checkboxes ("which
  dates work?"), chosen at creation.
- **Results are visible to guests** — counts + names per option, Facebook-style,
  consistent with the guest-list toggle's spirit. The organizer always sees full
  results on the dashboard.
- **Guests can add their own options** (per-poll toggle, default on): live
  immediately and auto-ticked for the adder — the trusted-guests model (§8), with
  the dashboard's remove-option as the moderation lever. Case-insensitive dedupe
  reuses an existing option; caps: 100 chars/option, 20 options/poll. This is the
  app's first guest→guest content surface — autoescaping + the caps bound it.
- **Lifecycle:** voting locks when the poll is closed, or when the event is past or
  cancelled (same gate as RSVP edits, §2.5); results stay visible. Votes are
  re-editable via the link any time while open; the submitted form is the whole
  truth (unticked = removed). Revoked envelopes drop out of counts and names, like
  every other number (§2.2).
- **No automatic notification** on poll creation — guests see it on next visit; the
  existing "update" send (§2.4) covers announcing it. Revisit if it grates.

## 3. Why build this — alternatives considered

Honest framing: this is a hobby/ownership project with real (but modest) utility gains
over off-the-shelf options. Recorded so scope stays honest.

- **Google Calendar — poor fit.** Email-keyed (most friends here are Messenger-keyed,
  and current emails aren't even known for all of them); invites read like corporate
  meeting requests and get ignored; non-Google guests get a clunky flow; no
  "who hasn't answered → nudge" loop, which is half the actual job.
- **Partiful / Luma — the real competition.** Free, and covers most of this product:
  event page, shareable link over any channel, no-account RSVP, reminders. What building
  adds over them:
  - **Per-guest tokenized links** — know who opened vs. ignored, not just one shared link.
  - **Data ownership** — friends' contact info and RSVP history on your hardware, not a
    startup's; no monetization of guests' phone numbers.
  - **No platform churn** — it runs as long as your infra does.
  - **The build itself** — a well-scoped learning project (auth, deliverability,
    capability URLs, self-hosting) with a payoff you use at your own parties.
- **Verdict:** utility delta over Google Calendar is large; over Partiful it's small and
  the honest justification is ownership + wanting to build it. Consequence for scope:
  **keep Phase 1 ruthlessly minimal** (event page, tokenized RSVP links, assisted
  Messenger/WhatsApp share, email via Resend — roughly two weekends) and treat everything
  beyond it (extra automated channels) as optional (§10).

## 4. Architecture — hub and spokes

The app is the **hub** (source of truth). Each delivery method is an interchangeable
**spoke**. Spokes can be added one at a time. Crucially, *all* channels do the same
job — deliver one link — and *all* responses come back the same way, through the RSVP
page. There is no inbound-message path to build.

```
                 ┌─────────────────────┐
                 │   Core App (hub)    │  ← source of truth
                 │  events + guests    │
                 │  + RSVP state       │
                 └──────────┬──────────┘
                            │  deliver a link
        ┌───────────────────┼───────────────────┐
        │                   │                   │
   ┌────▼────┐         ┌────▼────┐         ┌────▼────┐
   │  Email  │         │Messenger│         │WhatsApp │   ← spokes (channels)
   │ (auto)  │         │(assist.)│         │(assist.)│
   └────┬────┘         └────┬────┘         └────┬────┘
        │ link              │ link              │ link
        ▼                   ▼                   ▼
   ┌─────────────────────────────────────────────────┐
   │            RSVP web page (per-invitee)           │  ← all responses land here
   └─────────────────────────────────────────────────┘
```

### Components

1. **Core app** — web app to create events, manage guests, see who's coming. The only
   thing that "knows" real event state.
2. **Outbound dispatcher** — takes "invite person X to event Y" and figures out *how* to
   reach them per channel. Each channel is a plug-in behind one interface. Channels come
   in two flavors: **automated** (the app calls an API to send — email now; Telegram/SMS
   later) and **assisted** (the app prepares a share payload and a human taps send —
   Messenger and WhatsApp, §6). Every channel carries the same thing: a link to the RSVP
   page. (Email may additionally attach the plain add-to-calendar `.ics`, but that's a
   convenience, not a separate response path.)
3. **RSVP web page** — one unique link per person, tap Going / Maybe / Can't. Works for
   everyone regardless of channel, and is where *every* response comes back. This plus a
   single channel is already a complete product.

## 5. Data model (conceptual)

Core idea: **one person, many channels**, and **envelopes (invitations) vs. attendees
(the people actually counted)**.

*Concrete Django models: `core/models.py`.* Refinements settled while writing them:
**(a)** single-tenant — it's one household's self-hosted instance, so all organizers
share one dataset; no per-owner scoping, `created_by` is attribution only (§8).
**(b)** `plus_ones` lives on the **envelope** (invitation), not per attendee — matches the
one-stepper RSVP UX and simplifies headcount. **(c)** guest channel-change requests are a
`contact_channels` row with `status=proposed`, not a separate table (§2.5).

- **users** — organizers (and invitees who sign up).
- **events** — owned by an organizer. Holds a stable `ics_uid` used for the
  add-to-calendar file (so a guest re-adding an event dedupes in their calendar rather
  than duplicating). Plus the §2 state and toggles: `status` (draft/active/cancelled),
  `allow_plus_ones` (+ optional cap), `show_guest_list`.
- **contacts** — a person you might invite. *Not* necessarily a user — most invitees
  never make an account. Includes tags (§2.2) and an optional `household_id`.
- **households** — a named grouping of contacts ("The Hendersons") invited as one unit
  (§2.2). A contact belongs to at most one household.
- **contact_channels** — the ways to reach a contact (messenger / whatsapp / email /
  telegram / sms). One contact can have several. Each carries `source` (organizer /
  guest-requested) and `status` (`active` / `proposed`): guest channel-change requests
  sit as `proposed` until organizer approval flips them to active + preferred (§2.5).
- **invitations** — the **envelope**: targets exactly one contact *or* one household
  (CHECK: exactly one set). Holds the capability token, the invite `state` + `opened_at`,
  `plus_ones` (envelope-level — see below), and the denormalized `latest_note`.
- **invitation_attendees** — the **people inside the envelope**: one row per covered
  person (the single contact, or each household member) carrying that person's
  `rsvp_status` (going / maybe / cant / no-reply). A single-contact invitation has
  exactly one row — same shape everywhere, no special cases. Headcount = Going attendees
  (across this table) + `plus_ones` from envelopes with any Going attendee. Rows are
  **auto-created** from the envelope's target (idempotent `sync_attendees`, re-run when
  household membership changes) and never auto-removed — history survives someone
  leaving a household.
- **deliveries** — each actual send over a channel (for retries / multi-channel — a
  household envelope may deliver to several member channels). An **audit record**, not a
  queue (§9 — sends are synchronous): records the actual address/number used and the
  outcome.
- **rsvp_events** — append-only history of responses (attendee, status, note, timestamp,
  and `actor` = guest / organizer, §2.3); current status + latest note are denormalized
  onto `invitation_attendees` / `invitations`.
- **polls / poll_options / poll_votes** (§2.7) — a poll belongs to an event
  (question, `multi_choice`, `allow_guest_options`, `is_closed`); options carry
  `added_by` (invitation) when guest-added, empty for organizer ones (attribution +
  moderation); votes join **option × invitation** (one ballot per envelope — §2.7),
  unique per pair. Single-choice is enforced in the vote view's replace-all sync,
  not the DB — a constraint can't see `multi_choice`.

### Non-obvious decisions (where the bugs live)

- **Contacts ≠ users, and channels ≠ contacts.** Keeps invitees lightweight and lets the
  same person be reached multiple ways.
- **Envelope ≠ attendee.** The invitation carries the token and delivery state; the
  attendee rows carry RSVP state. A family shares one link, yet every person is counted
  and tracked individually — and the single-contact case is just an envelope with one
  attendee, not a separate code path.
- **RSVP writes have two sources, last-write-wins.** Status changes via the RSVP page
  (guest) or an organizer override (§2.3). Each appends to `rsvp_events` with its
  `actor`; the current value is simply the latest write, and the full history is kept.
  No sequence guards or reply-collision logic — there are no inbound replies to reconcile
  (§1 non-goals).
- **Deferred: recurring events.** Needs `RECURRENCE-ID` and per-occurrence RSVPs. Defer
  until the single-event flow is solid; **clone** (§2.1) covers the common case.

## 6. Channels

### Messenger & WhatsApp — assisted share, first-class (Phase 1)
Messenger matters most: it's how the organizer reaches the majority of friends. No API
allows *automated* sends to personal friends, but an **assisted share** gets within one
tap of seamless — so Messenger (and WhatsApp, below) are modeled as first-class channels
whose "transport" is a human tap instead of an API call.

**What's not possible (don't build it):** there is no API to programmatically message
your personal Facebook friends. The official Messenger Platform is Pages-only and
*reactive* — a Page can only message a user who messaged it first (24-hour window), and
the `CONFIRMED_EVENT_UPDATE` message tag is deprecated (error code 100) effective
27 April 2026. Unofficial login-based libraries (driving your personal account) violate
ToS and risk account bans — unacceptable for a recurring workflow. The old web **Send
Dialog** (`FB.ui method:'send'`) is desktop-only, needs a Facebook App ID + login, and
its `to` parameter expects app-scoped Facebook user IDs you won't have for friends. Skip
all of it.

**The assisted flow (Web Share API):**
- On mobile, a "Share via Messenger" button calls `navigator.share({ text, url })`,
  opening the OS share sheet with **Messenger as a target**. The organizer picks the
  friend; the invite blurb + that invitee's unique RSVP link arrive pre-filled in the
  Messenger compose box. One tap to send — and fully ToS-safe, since *the human* sends
  through the real app.
- A **send queue** UI walks through the assisted invitees one at a time (share → next →
  share → next), so a guest list of ~30 is a couple minutes of tapping, not a chore.
- **Desktop fallback:** a "Copy invite" button copies the pre-filled blurb + link to the
  clipboard to paste into messenger.com. (Simpler and more reliable than the Send Dialog.)

**WhatsApp: same assisted pattern, better targeting.** With a phone number stored, the
app renders a **`wa.me/<phone>?text=<blurb + link>`** deep link that opens WhatsApp
compose *directly to that person* with the message prefilled — no share-sheet
friend-picking, and it works from desktop too (hands off to WhatsApp Web). In the send
queue, WhatsApp invitees are "tap → send → next," the smoothest of the assisted
channels. The *automated* WhatsApp Business API stays out of scope (§10 Phase 2).

**Why this needs almost no backend:** every invite is just a unique tracking link, so
assisted channels require **zero inbound integration** — RSVPs return through the RSVP
page like any other channel (§4). The only new state is per-invitation **delivery
tracking**, which is *optimistic*: mark "shared" when the share sheet / deep link is
invoked (no delivery/read receipt comes back) and let the organizer correct it. The
true signal that it worked is the invitee clicking their link.

Telegram (and the WhatsApp Business API, if ever) remain Phase 2 options for fully
*automated* chat sends (§10).

### Email — transactional provider, never self-hosted SMTP
Sending from a residential IP is a deliverability graveyard (port 25 blocked, no IP
reputation). **Decision: use a transactional provider + a custom domain** (needed for
SPF/DKIM/DMARC). Volume is tiny (~30 invites/event, ~300–400 emails/month including
reminders), so cost is a non-issue — the goal is just reliable delivery from your domain.

- **Resend (decided).** 3,000 emails/month free, permanent, clean API. **No inbound
  parsing needed** — RSVPs come back through the RSVP page, not email — which is exactly
  why the provider choice is simple and free forever.
- **From address: your own domain.** Once the domain is verified (DKIM/SPF), sends come
  from any address you choose there — e.g. `Sam <invites@yourdomain.com>`. Nothing
  visible says "Resend". The address needs no real inbox (it's a sending identity), but
  set **`Reply-To`** to your personal inbox so a friend hitting Reply ("can I bring
  anything?") reaches a human instead of bouncing. Unverified accounts can only send
  from `onboarding@resend.dev` to themselves — test mode only. Optional polish later:
  send from a subdomain (`mail.yourdomain.com`) to isolate reputation.

## 7. Platform support

- The whole app is one **responsive web app** — event creation, guest list, RSVP
  dashboard, and the invitee RSVP page work equally on desktop and mobile browsers. The
  desktop/mobile split *only* affects the assisted-share hand-off (§6).
- **Mobile browser:** the good path. `navigator.share` opens the OS share sheet with
  Messenger as a target → one tap; WhatsApp deep links open the chat directly.
- **Desktop browser:** degrades gracefully. `navigator.share` support is inconsistent
  across desktop browsers, and even where it exists Messenger is rarely a share target —
  so desktop uses the **"Copy invite" → paste into messenger.com** fallback (3 steps,
  not one tap). WhatsApp still works via `wa.me`. Nothing breaks; it's just more manual.
  Practically: do the invite blast from a phone.
- **Not a native app.** Browser-based only — nothing in the App Store / Play Store. The
  optional **PWA** ("Add to Home Screen") adds a home-screen icon and full-screen feel;
  it unlocks **no** extra sharing capability (`navigator.share` behaves identically in a
  plain tab). A native app would only be justified by deeper OS integration this product
  doesn't need.

## 8. Auth & security

### Access model — two tiers, deliberately asymmetric
The app has two completely different kinds of user, and they get two different access
models. This is a core design decision, not an afterthought.

- **Organizer side — behind a login.** Creating, editing, or cancelling events, managing
  the contact list, and viewing the RSVP dashboard all require an authenticated session.
  Organizers are few (you, maybe a co-host or two) and trusted, and they control everyone
  else's personal data, so this side is locked down.
- **Invitee side — no login, capability URL.** The RSVP page requires no account. Access
  is granted purely by possessing a per-invitation **unguessable token** baked into the
  link (a "capability URL"): holding the link *is* the authorization to view that one
  invite and set that one person's RSVP — nothing else. This keeps invitee friction at
  zero, which §1 lists as a hard constraint.

**Organizer auth — decided: Cloudflare Access.** No auth code to write, no passwords to
store; an Access policy on the admin hostname allow-lists specific emails.

- **Additional organizers (e.g. spouse) need no Cloudflare anything.** Visitors are not
  Cloudflare users: an allow-listed person signs in with a **one-time PIN emailed to
  them** (or "Sign in with Google" if enabled) — no account, no app install. Session
  length is configurable so it's not per-visit. Anyone off the list is blocked at the
  edge before reaching the homelab.
- **Django wrinkle — two logins unless bridged.** Access controls who *reaches* the app;
  Django admin still has its own login. v1 options: (a) zero-code — just give each
  organizer a Django account too (two logins, fine); (b) **seamless (preferred)** — a
  small middleware (Django `RemoteUserMiddleware` pattern) validates the signed
  `Cf-Access-Jwt-Assertion` JWT header and auto-logs-in the matching Django user, so
  Access is the only login and Django still attributes actions per-user.
- **Safety requirement for (b):** the app must be reachable *only* through the tunnel
  (bound to the internal Docker network, no LAN/WAN port exposure) so the header can't
  be spoofed by hitting the app directly. This is the natural default in the Compose
  setup (§9).
- Full multi-user email+password / OAuth stays unbuilt unless genuinely needed later;
  the `users` table (§5) already anticipates it.

**Invitee token scheme (capability URLs).**
- Opaque, high-entropy (≥128-bit) random token per invitation — not sequential, not
  guessable, not derived from contact data.
- It's a **bearer capability**: anyone with the link can RSVP as that person. For casual
  social invites this is the accepted norm (Doodle, evite, Paperless Post all work this
  way). The tradeoff — a forwarded link lets someone RSVP on another's behalf — is
  acceptable here; mitigate by **not** exposing the full guest list or others' responses
  on the invitee page (the §2.1 "show who's coming" toggle shows first names only, off
  by default).
- Lifecycle: tied to the invitation, revocable by regenerating the token (or by
  uninviting, §2.2), optionally expired after the event.

### Security checklist
Proportionate to a small personal app — each item is worth a conscious decision. Ordered
roughly by what actually matters here.

1. **Protect the organizer account.** It controls every event and the whole contact list
   (real names, emails, phone numbers — PII). Strong auth (offloaded to Cloudflare
   Access) and rate-limit/lock any residual login. Highest-value target.
2. **XSS on the invitee page.** Event title, description, and RSVP notes are user input
   *rendered to other people*. Escape/sanitize all of it; set a strict
   **Content-Security-Policy**. The most likely real bug given multiple viewers.
3. **Capability-URL hygiene.** Tokens in URLs leak via `Referer` headers, server logs,
   browser history, and screenshots. Mitigate: `Referrer-Policy: same-origin` on the
   RSVP page, never log full tokens, HTTPS only, high entropy so brute-forcing is
   infeasible. (`same-origin`, not `no-referrer`: the latter makes browsers send
   `Origin: null` on the guest POSTs, which Django's HTTPS CSRF check rejects → 403.
   `same-origin` still sends nothing cross-origin, so the token never leaves our origin.)
   Rate limiting is done **at the edge, not in app code**: one Cloudflare
   WAF rate-limiting rule on `/i/*` (the free plan includes one rule) throttles
   token-guessing and abuse before it reaches the homelab. It covers the RSVP endpoint
   and the channel-change endpoint in one rule; the organizer side needs none (Access
   blocks unauthenticated traffic at the edge already).
4. **Guest-initiated channel changes are organizer-approved.** The RSVP page can request
   a new preferred channel (§2.5) from a *bearer* link, so nothing takes effect
   automatically: requests queue as `proposed` and a human approves each one. That
   review is the security gate — sanity-check the address/number plausibly belongs to
   that friend before approving (it catches typos too). A leaked link can't flood the
   queue: the endpoint sits under `/i/*`, covered by the edge rate-limit rule (item 3).
5. **PII / data protection.** You're storing friends' contact details. Collect the
   minimum, secure the DB (disk encryption if self-hosting; the Litestream backup
   target must be private). Single-tenant (§5), so all organizers legitimately share the
   contact list — the concern is protecting the whole dataset, not siloing organizers.
6. **SQL injection & input validation.** Parameterized queries only (the Django ORM
   default); validate event times, emails, phone numbers.
7. **CSRF.** RSVP submit and all organizer mutations are state changes — same-site
   cookies + CSRF tokens / capability-scoped POSTs (Django middleware default).
8. **Verify provider webhooks.** The **Resend bounce webhook** (§9) must be
   signature-verified so a forged POST can't flip delivery states; same rule for any
   later automated chat channels (Telegram etc., §10). This is the only inbound HTTP
   besides the RSVP page itself.
9. **Secrets.** API keys (Resend, etc.) in env/secret store, never in the repo. HTTPS +
   HSTS everywhere (free via Cloudflare Tunnel).

## 9. Tech & deployment

### App stack — Django + HTMX, no build step (decided)
Backend is Python (organizer's strongest language). Within Python, **Django** over
Flask/FastAPI, because this app's shape plays to Django's batteries:

- **Django admin ≈ free organizer backoffice.** Contacts, events, invitations,
  deliveries all get a generated CRUD UI — v1 needs hand-built pages only where they
  matter (RSVP page, dashboard, send queue — §2.6).
- **Security defaults match §8:** CSRF middleware, template auto-escaping (XSS), ORM
  parameterization, sessions — on by default rather than wired up by hand.
- **ORM + migrations built in, SQLite first-class** (enable WAL mode).
- Boring, stable, hugely documented — right property for a years-long self-hosted app.

**Frontend: server-rendered Django templates + HTMX + one hand-written CSS file.**
Server-rendered HTML is the right tool; HTMX covers the dynamic bits (dashboard updating
as RSVPs land; the send-queue share→next flow) with no JS framework, no npm, no build
step. PWA = a manifest + small service worker on top, stack-independent.
- **Guest pages use a bespoke stylesheet, not classless Pico.** The chosen guest-page
  look (see `mockups/` — sunset-gradient hero, frosted floating RSVP card, warm cards)
  is too custom for Pico's defaults, so the guest UI is hand-written CSS. This does *not*
  change the stack: still one static CSS file, no build tooling. Pico (or nothing) can
  still back the plainer organizer/admin screens where looks matter less.

**Background work: none in v1 — sends are synchronous (revised; cron dropped).** Every
notification in v1 is human-initiated (§2.4: invites on Send, nudges one-tap, the
day-before reminder is a confirmed prompt), so nothing runs on a clock and there is no
queue to drain. Hitting Send calls the provider *in the request* — Resend's batch
endpoint covers ~30 invites in one sub-second HTTP call — and the review screen
immediately shows per-guest ✓/✗. The `deliveries` table (§5) is therefore an **audit
record** (what went out, when, to which address, result), not a queue; failed rows get a
manual retry button. **Bounces** arrive after the request completes, so a small
signature-verified **Resend webhook** endpoint flips the delivery/invitation to bounced
(§8). Still no Celery/Redis — and no cron either. If scheduled auto-reminders are ever
built, add a worker-loop sidecar (or Django's Tasks framework) *at that point*.

**Dependencies + packaging: uv** (matching the sibling `../keep` project — `uv sync` /
`uv run`, `[tool.uv] package = false`, single-stage uv Dockerfile). Deployed as **one
Docker Compose stack** on Proxmox — `app` (gunicorn + WhiteNoise), `cloudflared` (tunnel),
`litestream` (SQLite backup). The app exposes no host ports; the tunnel is the only
ingress. One repo, one `docker compose up`.

*Step-by-step build order, concrete tooling files, and per-phase gates live in
`IMPLEMENTATION_PLAN.md`.*

*Alternatives considered:* FastAPI/Flask + Jinja2 — lighter but hand-rolls admin,
forms, CSRF, migrations for no gain at this scale (FastAPI's strength is APIs; this is
a server-rendered app). Go — nice single-binary deploys, wrong trade against Python
fluency. JS meta-frameworks — a build toolchain a mostly-static product doesn't need.

### Database — SQLite (decided; Postgres dropped)
The workload is tiny and write-light: a handful of events, ~30 invitees each, a few RSVP
writes per event, near-zero concurrency. That's the textbook SQLite case.

- Everything in the data model is ordinary relational SQL that SQLite handles fine in
  WAL mode. None of the Postgres-only features (LISTEN/NOTIFY, PostGIS, advanced JSON,
  stored procedures, high write concurrency) are needed here.
- **What it buys:** no separate DB process to run, back up, or patch; the whole database
  is one file. Self-hosting becomes radically simpler.
- **Pairing:** self-host → **SQLite file + Litestream** (continuous backup to cheap
  object storage). (If the managed fallback is ever taken: Cloudflare D1 / Turso — see
  Path B below.)
- **When to revisit:** if real concurrent multi-organizer write traffic ever appears,
  Postgres (Neon) is the clean next stop — the schema ports with minor dialect tweaks.
  Until a performance constraint actually fires, SQLite is correct.

### Deployment — decided: Path A, self-host on Proxmox
Email is *always* a hosted provider (§6) — never self-hosted SMTP. For the app itself,
**Path A (self-host + Cloudflare Tunnel) is the chosen path**; Path B stays documented
as the fallback if running it at home ever stops being fun.

**Path A — self-host on Proxmox** (chosen)
- App + RSVP page run at home: stateful, yours, on infra that already exists.
- Exposed via **Cloudflare Tunnel** (or Tailscale Funnel) — outbound tunnel, no port
  forwarding, home IP stays hidden, TLS handled.
- Full data ownership and no platform dependence — the reasons this project exists (§3).

**Path B — managed free tiers** (fallback, not chosen)
- App + RSVP page → **Cloudflare Workers** (free: 100k req/day). Serverless, so **no
  spin-down/cold-wake penalty** — a friend tapping an RSVP link after a quiet week still
  gets a sub-second response. No tunnel needed. (*Vercel Hobby* is an alternative but
  non-commercial only.)
- Database → **Cloudflare D1** (or Neon if Postgres returns).
- Zero maintenance and survives the homelab being down, but gives up the data-ownership
  rationale (§3) — kept only as an escape hatch.
- **Avoid** the free tiers that no longer hold up (details in §12): Railway, Fly.io,
  Koyeb, Render (DB expiry + cold-wake), Supabase (idle pause).

### Custom domain — required either way
**Decided: `samandmonevents.party`** (registered 2026-07-04; see CLOUDFLARE_SETUP.md §8
for the full record). Needed regardless of path: **Resend requires DKIM on your own
domain** (managed hosting
only provides a `*.workers.dev` subdomain for the app itself), and RSVP links need a
stable, trustworthy hostname.

- **Buy it anywhere** — the real requirement is just pointing its nameservers at
  Cloudflare (free), which is what makes Tunnel / Access / Workers / DNS work.
- **Cloudflare Registrar recommended but optional:** at-cost (registry + ICANN fee, no
  markup — ~$10.44/yr for `.com`), flat renewals, free WHOIS privacy + DNSSEC. Buying
  from Cloudflare is the only thing that *forces* Cloudflare nameservers. If buying
  elsewhere (Porkbun, Namecheap, …), confirm nameserver changes are allowed and watch
  for cheap-first-year-pricey-renewal pricing.

### Cost summary — the domain is the only bill

| Component | Cost |
|---|---|
| Cloudflare **Tunnel** (Path A exposure) | Free, no usage limits |
| Cloudflare **Access** (organizer login gate, §8) | Free up to 50 users (need 1) |
| Cloudflare **Workers** + **D1** (Path B) | Free tiers (100k req/day; 5 GB DB) |
| Cloudflare **DNS / SSL / CDN** | Free |
| **Resend** email (§6) | Free (3,000/mo) |
| Homelab compute (Path A) | $0 — hardware already exists |
| **Domain** | **~$10.44/yr** for `.com` at cost |

Total: **~$10/year.** The only thing that would ever add cost is Phase 2 automated SMS /
WhatsApp Business API, at per-message rates — if pursued.

## 10. Build order / roadmap

1. **Phase 1 — the complete product: core app + RSVP page + email + Messenger/WhatsApp
   assisted share.** The full functional spec in §2. Email sends links via Resend;
   Messenger uses the assisted share flow (Web Share API + send queue) and WhatsApp uses
   `wa.me` deep links (§6). All lead back to the one RSVP page, so no inbound of any kind
   is needed. Per §3: keep this ruthlessly minimal. **This is the whole product** — there
   is no larger system it's a stepping-stone toward.
2. **Phase 2 — additional *automated* channels (only if wanted).** Telegram first
   (easy/free), then SMS / WhatsApp Business API. Each is a new spoke behind the
   dispatcher interface. Assisted Messenger/WhatsApp already ship in Phase 1, so this is
   purely about removing the manual tap for channels that *can* be automated.

## 11. Open questions / TODO

**Open decisions**
- [x] Deployment path — **decided: Path A, self-host on Proxmox + Cloudflare Tunnel** (§9).
- [x] App stack — **decided: Django + HTMX, Docker Compose, SQLite; synchronous sends
      with `deliveries` as audit record — no cron/queue in v1** (§9).
- [x] Organizer auth — **decided: Cloudflare Access** (one-time PIN / Google for
      allow-listed emails; JWT→Django auto-login middleware preferred) (§8).
- [x] Native calendar Accept/Decline (ICS REPLY) — **decided: out of scope, permanently**
      (§1 non-goals). Responses only via the RSVP page.
- [x] Choose + register the domain — **decided: `samandmonevents.party`, registered
      2026-07-04**; tunnel `evently` connected. Remaining: DNS records (SPF / DKIM /
      DMARC) for Resend (runbook §5).
- [ ] Confirm the §2 functional-spec defaults: plus-ones on by default, "show who's
      coming" off by default, no RSVP cutoff, silent uninvite, cover images deferred,
      household RSVP editable by any link-holder.

**Phase 1 build details**
- [x] Full DDL / Django models with indexes + constraints — **drafted: `core/models.py`**
      (conceptual model §5, fields implied by §2). Migrations still to generate.
- [ ] Dispatcher interface so channels are pluggable — must support both *automated* and
      *assisted* channel types (§4/§6).
- [ ] RSVP page token scheme: pin entropy (≥128-bit), lifecycle, revocation (§8).
- [ ] RSVP page states & copy: fresh / already-responded / changed / cancelled / revoked
      (§2.5).
- [ ] Guest channel-change requests: RSVP-page picker (Messenger / WhatsApp / email) →
      `proposed` state → organizer approve/reject queue on the dashboard; approval flips
      the contact's preferred channel (§2.5/§8).
- [ ] WhatsApp assisted channel: `wa.me` deep-link format + prefilled text; store phone
      numbers normalized (E.164) so the links work (§6).
- [ ] Household RSVP UI: per-member Going/Maybe/Can't, shared note, any-holder-can-edit
      semantics; dashboard row expansion (§2.5/§2.6).
- [ ] Organizer RSVP override: per-attendee set status / plus-ones / note from the
      dashboard, written to `rsvp_events` with `actor=organizer`; last-write-wins vs. the
      guest (§2.3/§5).
- [ ] Add-to-calendar file: generate a plain `VEVENT` `.ics` (stable `ics_uid`) + a
      Google Calendar quick-add link on the RSVP page (§2.5).
- [ ] Notification templates per channel: invite, nudge, update, cancellation (§2.4).
- [ ] Assisted share: test `navigator.share({ text, url })` with the Messenger target on
      iOS + Android (does the link carry? text + link, or link only?). Define the desktop
      "copy invite" fallback and the send-queue UI.
- [ ] Delivery tracking: optimistic "shared" state on share-sheet/deep-link invoke, with
      an organizer override; treat the first RSVP-link click as the real delivery signal.
- [ ] Consider PWA packaging so Web Share is one tap from the phone home screen (§7).
- [ ] Security pass before first real use: CSP + escape user text (XSS),
      `Referrer-Policy` on the RSVP page, parameterized queries, CSRF, configure the
      Cloudflare WAF rate-limit rule on `/i/*`, secure the DB backup target (PII). See §8.
- [ ] Cloudflare Tunnel setup + reverse proxy in front of the app container (Path A);
      verify the app is unreachable except via the tunnel (required for the Access JWT
      auto-login, §8).
- [ ] Access→Django auto-login middleware: validate `Cf-Access-Jwt-Assertion`, map email
      → Django user (§8).

**Later / maybe (Phase 2)**
- [ ] Additional automated channels as new spokes: Telegram, then SMS / WhatsApp Business
      API (§10).
- [ ] Recurring events (deferred): `RECURRENCE-ID`, per-occurrence RSVP.

## 12. Reference notes

- Messenger event-update tag deprecation: error code 100, effective 27 April 2026.
- Resend free tier: 3,000 emails/month, permanent; no inbound parsing (not needed).
- DB free tiers (mid-2026): Cloudflare D1 — 5 GB, 5M reads/day, 100k writes/day,
  permanent, no card. Turso — 5 GB, 500M row-reads/month. Neon Postgres — 0.5 GB,
  scale-to-zero (~0.5–2s cold start), permanent, no card. SQLite WAL handles
  low-concurrency apps to ~100 GB.
- Hosting free tiers (mid-2026): Cloudflare Workers — 100k req/day, no cold-start sleep.
  Railway — no free tier ($5 one-time trial). Fly.io / Koyeb — free compute removed for
  new accounts. Render free web — spins down after 15 min (~30–50s wake); free Postgres
  expires after 30 days. Supabase free — Postgres pauses after 7 days idle.
