# Send queue / outbox — plan

Status: not built. Drafted 2026-08-10; all design questions settled (§3, §12).

## 1. The problem

Right now there is no single answer to "what has actually gone out for this event?"

- **Email** is sent synchronously inside the request. A `Delivery` row is created and
  flipped to `SENT`/`FAILED` in the same call (`channels.dispatch_email`), so the row is
  an after-the-fact receipt — there is never a moment where you can look at what is
  *about* to be sent.
- **Assisted (WhatsApp/Messenger)** sends have no row at all until the organizer taps
  share, at which point one is created already marked `SHARED` — optimistically, whether
  or not a message was really sent.
- **"Still to do" is derived, not stored.** `send_targets()` recomputes the outstanding
  work every request from invitation state + channel routing + a set of already-shared
  `(invitation, channel)` pairs. Skipped items live in the **session**, so the walk isn't
  durable and two devices disagree.
- **A `Delivery` doesn't record what the message was.** There is no `kind` field, so a
  row can't tell you whether it was an invite, a nudge or a cancellation.
- The work is split across three screens (Add guests, Send & notify, Send queue) with no
  place that lists everything.

## 2. The change, in one line

**`Delivery` stops being a receipt and becomes a real outbox.** Every message that needs
to go out — email included — is written as a `pending` row *before* anything is sent.
The per-event **Messages** screen is that outbox: the full list, every status, one button
to send all pending email, and a checklist plus a card walkthrough for the manual ones,
each marked done by hand.

## 3. Decisions taken

| Question | Decision |
|---|---|
| Do actions still send, or only enqueue? | **Enqueue only.** Adding guests, nudge, update, cancel, reminder all create `pending` rows and send nothing. Email leaves only when you press *Send all pending emails*. |
| Message text | **Snapshot at enqueue, editable per row.** Each row stores its own subject + body; you can edit one guest's message before it goes. |
| Guests with no usable channel | **Rows in the queue**, status `blocked`, so the list really is complete. |
| Manual working mode | **Checklist + card walkthrough**, both acting on the same rows. |

Consequence to accept up front: **"adding is inviting" is gone** (design doc §2.3). Adding
guests queues their invites; a loud "N messages waiting to send" prompt on the dashboard
and the event header is a required part of this change, not a nice-to-have.

## 4. Where I'd deviate from the proposal

Four refinements, all aimed at matching the existing data model rather than bolting a
second one alongside it:

1. **No new model.** `Delivery` already is one row per (envelope, channel) send attempt
   with an address snapshot, a provider id, an error and a `sent_at`. Adding
   `message_kind`, a `pending` status and the text snapshot turns it into the outbox
   without a parallel table to keep in sync. The UI calls them "messages"; the model
   keeps the name `Delivery` to keep the diff honest and small.
2. **Snapshot, but re-render when untouched.** A pure snapshot has a stale-content trap:
   fix a typo in the event, and every queued invite still carries the old wording. So a
   row also carries `is_edited`. On send, an **unedited** row is re-rendered from current
   event data and its snapshot overwritten; an **edited** row goes verbatim. You get
   previewability and per-row editing without a silent staleness bug.
3. **Statuses collapse; `shared` disappears from `Delivery`.** Whether a send was
   automated or manual is already recorded by `kind` (email vs whatsapp/messenger), so a
   separate `shared` status is redundant — and under the new rules a manual row reaching
   `sent` means *you told us you sent it*, which is a stronger claim than today's
   optimistic `shared`. The invitation-level ladder keeps `State.SHARED` (it is
   guest-progress vocabulary and appears all over the dashboard); it's derived from
   `delivery.kind` at mark-sent time.
4. **`send_targets()` shrinks to an audience selector.** Once outstanding work is stored,
   the queue is `Delivery.objects.filter(invitation__event=event, status=PENDING)` — a
   query, not a computation. `shared_channel_pairs()`, `pending_assisted`,
   `non_responders_assisted`, `notified_assisted`, `reminder_assisted` and the whole
   email-vs-assisted split all delete. What survives is "who is the audience for a
   nudge?", which is genuinely per-action logic.

## 5. Model changes

### 5.1 `Delivery` — new and changed fields

```python
class Delivery(TimestampedModel):
    """The outbox: one row per (envelope, channel, message) that needs to go out.

    A row is created **pending** before anything is sent. Email rows are dispatched in
    a batch by the Send-pending-emails button; assisted rows are marked sent by hand
    once the organizer has actually shared them.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"        # queued, nothing sent yet
        SENT = "sent", "Sent"                 # email: provider accepted / manual: organizer confirmed
        FAILED = "failed", "Failed"           # send attempt errored — retryable
        BOUNCED = "bounced", "Bounced"        # provider bounce webhook
        BLOCKED = "blocked", "No channel"     # nobody to send to; needs a channel or a hand-wave
        CANCELLED = "cancelled", "Cancelled"  # organizer decided not to send it

    class Kind(models.TextChoices):           # message kind, not channel kind
        ...  # invite / nudge / update / cancellation / reminder  (mirrors messaging.KINDS)

    # existing: invitation, channel, kind (channel kind), address_used, status,
    #           provider_message_id, error, sent_at
    message_kind = models.CharField(max_length=20, choices=MessageKind.choices)
    subject = models.CharField(max_length=255, blank=True)   # snapshot at enqueue
    body = models.TextField(blank=True)                      # snapshot at enqueue (text)
    is_edited = models.BooleanField(default=False)            # organizer touched the text
    deferred_at = models.DateTimeField(null=True, blank=True) # "skip for now", persisted
    sent_by = models.ForeignKey(AUTH_USER_MODEL, null=True, blank=True, on_delete=SET_NULL)
```

Naming note: `kind` currently means *channel* kind. Rename it to `channel_kind` in the
same migration so `message_kind` isn't ambiguous — it's referenced in ~8 places.

### 5.2 Constraints and indexes

```python
constraints = [
    # Don't double-queue the same message to the same channel.
    UniqueConstraint(
        fields=["invitation", "channel", "message_kind"],
        condition=Q(status="pending"),
        name="one_pending_message_per_channel",
    ),
    # Same guard for blocked/no-channel rows, where channel is NULL and SQLite would
    # otherwise treat every NULL as distinct.
    UniqueConstraint(
        fields=["invitation", "message_kind"],
        condition=Q(status="pending", channel__isnull=True),
        name="one_pending_blocked_message_per_invitation",
    ),
]
indexes = [Index(fields=["status", "message_kind"])]
```

### 5.3 Data migration for existing rows

- `shared` → `sent` (keep `sent_at`).
- `queued` → `failed`, `error = "left queued by the pre-outbox sender"`. Deliberately not
  `pending`: those rows are historical debris and must not silently resend.
- `message_kind` backfills blank; the UI renders blank as "—" for pre-migration rows.

### 5.4 What doesn't change

`Invitation.State` and its monotonic ladder, `InvitationAttendee`, `RsvpEvent`,
`ContactChannel`, polls. `Invitation.State.QUEUED` finally becomes meaningful: `pending`
= invited, nothing queued; `queued` = a message is waiting in the outbox; `sent`/`shared`
= it went out.

## 6. Message rendering

`messaging.py` needs a small refactor so an edited body can still be wrapped for email:

- Make `_subject_and_text()` public as `render(kind, invitation, url) -> (subject, text)`.
- Make `_html()` public as `html_wrap(text, url, message_kind)`.
- **Bug to fix while there:** `_html()` assumes the last paragraph is the bare URL and
  drops it. On an edited body the URL may sit anywhere. Change it to strip any paragraph
  that is exactly the URL and always append the button.
- **Invariant guard:** the link is the entire point of every message (§4). Reject a saved
  edit whose body no longer contains the invitation's RSVP URL, with an inline error and
  a "restore the link" hint.

Send-time rule, in one place (`_body_for_send(delivery)`):

```
if delivery.is_edited:  use delivery.subject / delivery.body verbatim
else:                   re-render from the event now, overwrite the snapshot, then send
```

## 7. Flows

### 7.1 Enqueue

New in `channels.py`:

```python
def enqueue(invitations, message_kind, base_url, *, actor=None) -> dict:
    """Write pending outbox rows. Sends nothing. Idempotent: an invitation that already
    has a pending row for this message_kind + channel is skipped."""
```

Per invitation:

- `channels = email_channels(inv) + assisted_channels(inv)` (both helpers stay as they
  are — they already do the routing and dedup work correctly).
- No channels → one `blocked` row (`channel=None`, `channel_kind=""`).
- Otherwise one `pending` row per channel, subject/body snapshotted via
  `messaging.render()`, skipping channels that already hold a pending row of this kind.
- `invitation.advance_state(QUEUED)`.

Returns `{"queued": n, "blocked": n, "already_queued": n}` for the banner.

Callers:

| Screen / action | Today | After |
|---|---|---|
| Add guests (`event_invite`) | creates invitations + emails immediately | creates invitations + `enqueue(..., "invite")`, redirect to Messages |
| Send & notify → invites | `dispatch_email` | `enqueue(targets["invites"], "invite")` |
| … nudge / update / reminder | `dispatch_email` | `enqueue(...)` with the same audience |
| … cancel | sets status + emails | sets status, **cancels every other pending row for the event**, then `enqueue(..., "cancellation")` |
| … retry | re-sends | flips `failed`/`bounced` rows back to `pending` |
| Per-guest resend (`invitation_action`) | `dispatch_email([inv], kind)` | `enqueue([inv], kind)` |
| Uninvite (revoke) | — | cancels that invitation's pending rows |
| Editing a live event | — | **nothing automatic.** No `update` message is queued on your behalf; you queue one from Send & notify when you decide the change is worth telling people about |

### 7.2 Send pending email

```python
def send_pending_emails(event, base_url, *, actor=None) -> dict:
```

Takes **every** `status=PENDING, channel_kind=EMAIL` row for the event regardless of
message kind, batches by 100 exactly as `dispatch_email` does now (that batching +
partial-response handling is good and is kept verbatim), marks each `sent`/`failed`,
advances the invitation to `SENT`. Returns `{"sent": n, "failed": n}`.

No kind or subset parameter: holding a message back is done by cancelling its row
(*Don't send*) before pressing the button, so the button always means "send what the
Pending email list currently shows". The kind/status filters on the page filter the
*display* only — the count on the button is the true pending total, never a filtered one.

### 7.3 Mark a manual message done

`mark_sent(delivery, user)` → `status=sent`, `sent_at=now`, `sent_by=user`, clears
`deferred_at`, and advances the invitation to `SHARED` (assisted channel kind) or `SENT`
(email row handled by hand, e.g. sent from your own inbox). Nothing is ever marked sent
by the app on the organizer's behalf.

`blocked` rows can also be marked sent — "I told them at the school gate" — recording the
row as done with no channel.

### 7.4 Other row actions

`defer` (sinks it to the bottom, persisted — replaces the session skip set), `cancel`
(status `cancelled`), `retry` (`failed`/`bounced` → `pending`, clears `error`), `edit`
(subject/body, sets `is_edited`), `revert to template` (clears `is_edited`, re-renders).

## 8. Screens

### 8.1 Event → Messages — `/admin/events/<pk>/messages/`

The one place. Old `/queue/` 302s here to keep bookmarks alive.

```
Messages — Sam's 40th
┌──────────────────────────────────────────────────────────┐
│  6 pending email   4 manual to do   1 can't send         │
│  22 sent   1 failed   0 bounced                          │
└──────────────────────────────────────────────────────────┘

Pending email — 6                             [ Send all 6 ]
  ▸ kate@…        Kate Henderson    invite
  ▸ dave@…        Dave Patel        invite
  ▾ jo@…          Jo Patel          nudge          edited
      Subject: Still hoping you can make it — Sam's 40th
      Hi Jo, just a friendly nudge about Sam's 40th (Sat 6 Sep,
      7:00 PM) — it'd be great to know either way.
      Tap to reply (takes 5 seconds): https://…/i/AbC…
      [Edit] [Revert to template] [Don't send]
  …

Manual sends — 4 to do                    [ Work through them → ]
  ▸ The Hendersons · WhatsApp · invite  [Share] [Mark sent] [Edit] [Defer]
  ▸ Jo Patel       · Messenger · invite [Share] [Mark sent] [Edit] [Defer]
  …

Can't send — 1
  Auntie Ngaire · no channel · invite  [Add a channel] [Mark done]

Needs attention — 1
  dave@… · invite · failed: provider timeout            [Retry]

All messages  [invite ▾] [any status ▾]
  ✓ sent   Mon 09:14  kate@… · invite · email
  ✓ sent   Mon 09:14  The Hendersons · invite · WhatsApp (by Sam)
  …
```

**Row lifecycle on this page.** A row lives in exactly one section, decided by its status:

```
  queued  →  Pending email / Manual sends / Can't send
              │
   Send all → ├─ sent ────────→ All messages          (with a sent timestamp)
              └─ failed ──────→ Needs attention       [Retry] → back to pending
  Don't send → cancelled ─────→ All messages          (greyed, "not sent")
  bounce webhook: sent → bounced → Needs attention
```

So yes — pressing **Send all 6** empties the Pending email section and those six rows
reappear under All messages as `sent`, except any that errored, which land in Needs
attention with the provider's message and a Retry button. The post-send banner reads
"6 sent · 0 failed" and All messages highlights the just-sent rows briefly.

- **Pending email is a real list, not just a count** — one row per recipient, collapsed to
  address + name + message kind. Expanding a row (`▸`) shows the exact subject and body
  that will be sent, with an "edited" badge where the text was customised.
- **Send all sends every pending email row**, across message kinds. To hold one back you
  press **Don't send** on it first, which cancels that row and drops the count to 5. No
  tick-boxes: one button, one obvious action.
- The three outstanding sections are the top of the page and are ordered by what needs a
  human: email (one button), manual (a walk), blocked (needs a decision). Sent history
  lives below in All messages, filterable by kind and status.

### 8.2 Card walkthrough — `/admin/events/<pk>/messages/walk/?n=`

The existing `queue.html` card, re-pointed at a `Delivery` pk instead of an
`(invitation, channel)` pair. Buttons: **Share** (wa.me deep link / `navigator.share` /
copy), then **Mark sent & next**, plus **Skip for now** (sets `deferred_at`). Ordering:
pending manual rows, `deferred_at` nulls first, then oldest first. Session skip helpers
(`_skip_session_key`, `_get_skips`, `_add_skip`, `_reset_skips`) delete.

### 8.3 Send & notify → "Queue messages"

Same screen, verbs change: "Queue nudge for 12 guests", "Queue update for 23 guests". On
submit it redirects to Messages with "12 messages queued · 8 email, 4 manual".

### 8.4 Dashboard

Replaces the assisted-only prompt with an outbox prompt: **"11 messages waiting to send"**
→ Messages. Per-guest rows show the latest message status rather than only bounces.

## 9. Edge cases to get right

- **Idempotent enqueue.** Pressing "queue nudge" twice must not double-send; the partial
  unique constraint plus a pre-filter make the second press a no-op reported as
  "already queued".
- **Cancelling an event** must cancel pending invites/nudges/reminders first — sending an
  invite after a cancellation notice is the worst failure mode here.
- **Uninviting** cancels that envelope's pending rows.
- **Household with two channels** = two rows, two edits if you edit. Called out in the UI
  ("same link as another member") as the card already does.
- **Deleting a contact channel** (`_save_channels` deletes rows) `SET_NULL`s the delivery.
  A *pending* row whose channel just vanished must be re-pointed or moved to `blocked` —
  add that to `_save_channels`.
- **Guest-proposed channel approval** should offer "queue their invite on the new
  channel" if their existing row is `blocked` or `bounced`.
- **Bounce webhook** keeps flipping `sent → bounced`; the row then shows up under Needs
  attention with Retry, which requires picking a different channel to be useful.
- **Event edited after queueing**: unedited rows silently pick up the new text at send;
  edited rows show a "written before the event changed" warning on the Messages page
  (compare `event.updated_at` to `delivery.updated_at`).

## 10. Build order

| Phase | Work | Rough size |
|---|---|---|
| 1 | Model: `message_kind`, status overhaul, `subject`/`body`/`is_edited`/`deferred_at`/`sent_by`, `kind`→`channel_kind` rename, constraints, data migration | ~1 migration + model edits |
| 2 | `messaging.render()` / `html_wrap()` refactor + URL-preserved HTML fix | small |
| 3 | `channels.enqueue()`, `send_pending_emails()`, `mark_sent()`; delete `dispatch_email`, `shared_channel_pairs`, the assisted halves of `send_targets` | medium |
| 4 | Messages screen (view + template) with the four sections, filters, row actions | largest chunk |
| 5 | Card walkthrough re-pointed at `Delivery`; session-skip code deleted | small |
| 6 | Callers: Add guests, Send & notify, per-guest actions, uninvite, cancel-event cascade, dashboard prompt | medium |
| 7 | Per-row edit + revert, with the link-present validation | small |
| 8 | Tests + design doc §2.3/§2.4/§5/§6/§9 updates | medium |

Phases 1–3 are shippable behind the existing screens (enqueue + an immediate
`send_pending_emails` call keeps today's behaviour) if a half-way commit is wanted.

## 11. Test plan

Existing files needing rework: `tests/test_sending.py` (asserts synchronous send),
`tests/test_assisted.py` (asserts optimistic `SHARED`), parts of `tests/test_organizer.py`
and `tests/test_dashboard.py`. `tests/test_webhook.py` is nearly unaffected.

New coverage:

- Enqueue creates rows and sends nothing; second enqueue is a no-op.
- No-channel guest gets exactly one `blocked` row.
- `Send pending emails` sends only pending email rows, batches >100, marks failures.
- Manual row is only `sent` after an explicit mark; sharing alone leaves it `pending`.
- Editing a row makes the send use the edited text; unedited rows pick up an event edit.
- Editing out the RSVP link is rejected.
- Cancelling an event cancels pending invites before queueing the cancellation.
- Uninvite cancels pending rows.
- Deferred rows sink and survive a new session.

## 12. Settled questions

All four are answered; kept here with their reasoning so the decisions aren't re-litigated.


1. **Scope** — per-event only, or is a global "everything pending across all events"
   outbox wanted too? (Cheap to add later; the query is the same minus the event filter.)

   Answer - Per event is fine. do not need a global list
2. **Send-all granularity** — should *Send all pending emails* always send every kind at
   once, or should it respect the current kind filter (e.g. send only queued nudges)?

   Answer — settled: sends everything pending across all kinds; the page filters affect
   the list only, and a message is held back by cancelling its row (§7.2, §8.1).

3. **Auto-queue on event edit** — should a material edit to a live event automatically
   queue an `update` message to everyone notified?

   Answer — no. Nothing is queued on your behalf; queueing an update stays a deliberate
   action on Send & notify (§7.1). This keeps one rule true everywhere: **a row exists
   because you asked for it**. It also avoids the "fixed a typo in the address, 30 update
   messages appeared" trap. The related staleness case is already handled the other way —
   unedited pending rows re-render at send time, so a live edit reaches anyone whose
   message hasn't gone yet without a second message being queued (§4.2, §9).

4. **Retention** — sent rows accumulate forever.

   Answer — fine. No pruning, no archive job. At a few hundred rows per event this is
   never a performance question, and the full history is the point of the screen.
