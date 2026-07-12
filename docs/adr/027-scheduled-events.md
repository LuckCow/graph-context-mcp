# ADR 027: Scheduled Events — timed prompts as graph nodes

Date: 2026-07-12
Status: accepted (amended 2026-07-12: human-facing property surface +
status lifecycle — see "Amendment" below)

## Context

Everything the system does today is reactive: a turn happens because a
human posted a chat message. Dogfooding keeps producing requests shaped
like "remind me a week before taxes are due", "check back on this next
Friday", "every Monday, review the stale summaries" — the user wants the
assistant to *initiate* at a known future time, with instructions decided
now.

The pieces were nearly all in place. `Orchestrator.handle_message` is the
one transport-agnostic turn entry (WP6); the Anytype bot already runs
periodic background tasks that take the per-space turn lock
(`_watch_graph`, the WP8 chat watcher); sessions are keyed so a turn can
be addressed to a specific chat (ADR 021); and the `gc_` infrastructure
types show exactly how to keep bookkeeping nodes out of story traversal
while humans still see and edit them in Anytype (ADRs 008/015/021). What
was missing: a place to store "when + what", something to watch the
clock, and a way to hand the stored instructions to the model as a turn.

Constraint: the devcontainer's egress firewall means no new dependencies
— cron parsing had to be implemented in-repo.

## Decision

**A Scheduled Event is a node.** A new infra type, `gc_scheduled_event`
(display "Scheduled Event", role `ScheduledEvent`, member of
`INFRA_ROLES`), carrying five minted properties (keys are `gc_` for wire
stability; each mints under a human display name, shown in parentheses —
people edit these fields directly in the Anytype editor):

- `gc_schedule` ("Schedule", text) — WHEN, auto-detected by shape: an
  ISO local date-time (`2027-04-08T09:00`, one-shot) or a five-field
  cron line (`0 9 * * 1`, recurring; the classic `*`/ranges/steps/lists
  subset with the vixie day-OR-weekday rule). All times are the
  server's **local wall clock** — cron lines have no timezone, so a
  one-shot with a UTC offset is rejected with guidance rather than
  silently converted. (An empty/`off` schedule is inert.) "Local" means
  the **user's region**, not the container's clock (containers default
  to UTC): the devcontainer pins `TZ`, and `GC_TIMEZONE` (an IANA name,
  validated loudly at startup via `scheduler.local_clock`) overrides the
  system clock for the scheduler independently of `TZ`.
- `gc_schedule_prompt` ("Schedule prompt", text) — the instructions the
  LLM receives when the event fires, written to be self-contained.
- `gc_schedule_status` ("Schedule status", select) — the lifecycle
  switch: **Pending** events are scanned and fired; **Completed** /
  **Cancelled** are inert. Empty reads as Pending (a human creating the
  object must not need our vocabulary), and any unknown value stays
  active — only an explicit completion word deactivates. The scheduler
  owns the transitions: Pending at creation, Completed when a one-shot
  fires (recurring stays Pending), Cancelled on the tool's cancel. A
  human re-enables an event by flipping the status back to Pending.
- `gc_last_fired` ("Last fired", text) — the scheduler's bookkeeping
  stamp, on the node so it survives restarts and is repairable in the
  UI.
- `gc_session_key` ("Session key", reused from ADR 021) — which chat
  the fired turn belongs to.

These values live in the **real properties, never the `gc_fields`
blob**: the registry's blanket `gc_`-prefix exclusion gets an explicit
allowlist (`GC_REFLECTED_FIELD_KEYS`) covering this surface, so both
the write path and field reflection route natively — the person sees
filterable, sortable, editable fields, and can build Set views over
them ("pending reminders"). When the type is first minted, bootstrap
also seeds an **"Example Scheduled Event"** explainer object (the
example-mode pattern) whose empty schedule can never fire.

Because events are ordinary objects, **humans create and edit them in
Anytype** (the properties are attached inline at type creation so the
editor shows the fields) and the existing `_watch_graph` resync makes
those edits visible to the scheduler within a minute. Because the role
is infra, they stay invisible to explore/find_node/stats/semantic unless
explicitly named (`query type="ScheduledEvent"` — the standard escape
hatch).

**Timing rules are pure domain** (`domain/scheduling.py`: parse,
`next_fire`, `due_at` — no clock, no I/O, dependency-free cron). The
load-bearing semantics:

- a one-shot fires once; editing its schedule to a later time re-arms it
  (`due when last_fired < at <= now`);
- a recurring event must be **armed** — anchored by a first
  `gc_last_fired` stamp — before it can fire, so a fresh "every Monday
  09:00" waits for Monday. The `schedule` tool arms at creation;
  UI-created strays are armed (stamped, not fired) by the watcher;
- downtime collapses: however many occurrences were missed, there is one
  earliest due moment after `last_fired`, hence **one late fire**.

**The LLM manages events through a ninth tool, `schedule`**
(set/list/cancel), bound like `context` in every mode — a reminder
request is session bookkeeping, not graph authorship, so read-only modes
keep it. The tool stamps the current session's key on the node, echoes
the computed next-fire time *and the current server time* (the model
doing "a week before April 15" math can verify itself), and its errors
teach both schedule syntaxes. `application/scheduler.py` is the
use-case service (repository-direct like `CaptureRecorder`: no recent-
trail pollution, journalled so provenance links the intent); `Services`
gained the `session_key` it serves.

**Firing is a third watcher in the Anytype bot** (`_watch_schedule`,
`GC_SCHEDULE_TICK_SECONDS`, default 30, `off` disables). Each tick scans
the shared index (pure read), then for each due event: **mark
`gc_last_fired` first** (at-most-once — a crashing turn must not re-fire
every tick; its error still reaches the chat through the reply surface),
then run `handle_message(session_id="anytype:<chat>",
user_id="system:scheduler", text=scheduled_prompt(...))` under the
route's turn lock, and post the finished reply. Unlike user turns there
is **no "Processing…" placeholder** — nobody is waiting on a turn they
didn't start, so nothing appears in the chat until the reply is ready
(errors post the same way, as plain messages). `scheduled_prompt`
(pipeline) frames the turn:
the model is told the scheduler — not a user — woke it, and to act on
the stored instructions in chat. Delivery targeting: the event's own
chat when it is served in the space, else the space's first served chat
(deterministic); a space with no chat retries next tick rather than
dropping the event.

## Consequences

- "Remind me a week before to pay taxes" now works end-to-end: the model
  computes the date (the tool echoes the clock), stores the prompt, and
  a week before, the bot opens a turn in the same chat and the model
  delivers the reminder — and can consult the graph first.
- The MCP stdio server and the CLI can create/list/cancel events but
  nothing fires there — only the long-running bot has a clock loop. An
  event created over MCP carries key `mcp` and is delivered to the bound
  space's default chat. Same for Discord-keyed events for now; a Discord
  firing loop is future work.
- At-most-once means a crash between marking and replying can eat a
  fire; the error posture (an `[error] …` message posted to the chat)
  makes that visible. The alternative — at-least-once — risks a
  crash-looping turn re-firing forever, which is worse for a chat
  surface.
- One more infra type to bootstrap; `ensure_schema` now lists properties
  before minting types so inline property attachment stays idempotent on
  upgraded spaces.
- The fired prompt enters conversation memory as a user-role message,
  so follow-up turns remember the reminder happened.

## Alternatives considered

- **A separate store (JSON file, SQLite) for schedules** — rejected:
  Anytype is the human surface; a reminder the user can't see or edit in
  their own space breaks ADR 001's contract, and the graph already gives
  us persistence, resync, and provenance for free.
- **croniter/APScheduler** — blocked by the egress firewall, and the
  needed subset (5-field vixie cron, next-occurrence) is ~100 lines of
  pure domain logic that tests in milliseconds.
- **Natural-language schedules** ("next Friday") — the LLM is already
  the natural-language layer; it translates to ISO/cron at `set` time,
  and the echo lets it verify. Parsing English in the domain would
  duplicate the model's job, badly.
- **Firing as a synthetic chat message** (bot posts the prompt, then
  answers itself) — noisy and fights the echo suppressor; injecting the
  turn directly and posting only the reply keeps the chat clean.
- **UTC or tz-aware schedules** — cron has no timezone; mixing aware and
  naive datetimes is a bug factory. One convention (server-local, naive)
  with loud rejection of offsets is simpler and matches what "9am
  Monday" means to the user.

## Amendment (2026-07-12): human-facing properties + status lifecycle

Dogfooding the same day surfaced two gaps. First, although bootstrap
minted real properties, the registry's blanket `gc_`-prefix exclusion in
`reflects_field` silently routed every scheduler write into the
`gc_fields` JSON blob (and hid the properties from reads) — invisible
and uneditable to a human. Both paths route through `reflects_field`,
so one allowlist (`mapping.GC_REFLECTED_FIELD_KEYS`) fixed write and
read together; a contract test now pins that the raw stored object
carries the values as properties and an empty blob. Second, there was
no at-a-glance lifecycle: the "Schedule status" select (above) was
added, cancel became a status transition that **preserves the
schedule** (re-enable = flip to Pending in the UI; the old
`schedule="off"` clobber is retired as the cancel mechanism, though an
`off`/empty schedule still reads as inert), and properties gained
display names plus the seeded example object so people can author
events entirely in Anytype.
