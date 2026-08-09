# ADR 055: Scheduled Events — per-event modes and simple messages

Date: 2026-08-03
Status: accepted (amends ADR 027)

## Context

ADR 027's firing path injects a due event's turn through
`handle_message`, which resolves the mode from the chat's persisted
session state — so a scheduled turn runs in whatever Activity Mode the
chat *happens* to be in when the clock strikes. Dogfooding surfaced two
problems with that:

1. **The ambient mode is the wrong mode.** A "compile the Monday
   newsletter" event needs a specific mode — one with web search, the
   right model, the right tool binding — not whatever the chat drifted
   into since the event was created. The event's author knows the job;
   the chat's current mode is an accident of timing.
2. **Most reminders don't need a model at all.** A large share of
   events are plain reminders whose wording is already known at
   creation time ("Taxes are due April 15 — file this week."). Waking
   an LLM to paraphrase a known sentence costs a turn, adds latency,
   and risks the model rewording what the user wanted said verbatim.

## Decision

### An event carries exactly one of a message or a prompt

Two new REAL properties on `gc_scheduled_event` (same
`GC_REFLECTED_FIELD_KEYS` surface as the rest, display names "Schedule
message" / "Schedule mode", documented in the seeded explainer):

- **`gc_schedule_message`** — a *simple* event: at fire time the text
  posts to the chat VERBATIM, with no model turn. The transport marks
  the event fired (same at-most-once ordering, under the route lock),
  posts through `TurnReply.deliver` — no placeholder was ever opened,
  so it is a plain send whose id lands in the sent ledger and the bot
  never answers its own reminder — and then remembers the text as an
  assistant message via `Orchestrator.note_scheduled_post`. Without
  that last step the post would be invisible to the model live yet
  *appear* after a restart (startup seeding replays bot posts);
  memory must match the chat either way. No turn ran, so no intent
  node and no turn-log records — `gc_last_fired` plus the bot's log
  line are the record.
- **`gc_schedule_mode`** — a *prompt* event's Activity Mode, stored as
  the mode NAME (an opaque text label, the `SessionState.mode`
  precedent). Deliberately text, not select (options would go stale —
  mode names are live space data) and not an objects relation (an id
  needs registry resolution the application-layer Scheduler cannot do,
  plus `SYSTEM_RELATION_DENYLIST` machinery; a text name survives a
  mode object being archived and recreated, and a typo degrades
  safely instead of dangling).

`Scheduler.set` enforces exactly-one-of at the tool boundary with
errors that teach the distinction. A human can still store both in the
Anytype UI: **the message wins** — the rule lives in one place
(`Scheduler.tick` emits a `DueEvent` with exactly one populated) and
`list` surfaces the conflict with a "clear one" warning. The
alternative (inert-until-fixed, like a bad schedule) silently eats a
reminder, the worst failure mode for this feature; posting the human's
exact words is deterministic and harmless. Neither set keeps ADR 027's
behavior: an LLM event whose prompt falls back to the node name.

### Scheduled turns are always mode-pinned — never ambient

`handle_message` gains `mode: str | None = None`. `None` — every
conversational caller — is byte-identical to before: the session's own
mode. A string is a **per-turn pin**, resolved by the pipeline's
`_override_spec`: `registry.get(slugify(name))`, with unknown names
degrading to the registry default (one warning; the scheduler fires
unattended, so it must not raise) and empty pinning the default
silently. The pin never touches `_SessionState.mode` or its persisted
mirror — the next conversational turn is back in the chat's own mode.
`run_scheduled` **always** passes `mode=due.mode`, even when empty:
a scheduled turn never runs the chat's ambient mode again. Everything
keyed off the resolved spec follows for free — tool binding, goal,
`DecideOptions` (model/thinking/search), the diary's prompt
fingerprint (re-logs on the pin and back), and the intent node's mode
stamp, which now records the mode that actually ran.

**This changes behavior for pre-existing events**: an event with no
mode field now fires in the space's default mode (`gc_default_mode`,
ADR 034), not the chat's current mode. Deliberate — the ambient mode
was the bug, and the default mode is the space's stated baseline.

### Set-time mode validation by late-bound vocabulary

The application-layer Scheduler cannot see the orchestrator's
`ModeRegistry` (layering). It grows a public
`mode_names: Callable[[], Sequence[str]] | None` attribute; the
orchestrator's assembly late-binds it to
`lambda: orchestrator.registry.names()` — the `services.historian`
pattern, one bind covering every derived session because the scheduler
is shared by reference, and the closure reads the live registry so
`/mode` and change-tick reloads are honored. With the vocabulary wired,
`set` rejects unknown modes naming the loaded ones (errors are
prompts); the bare MCP server never binds it, so there `set` stores the
name unchecked and the fire-time degrade covers typos.

### The tool surface

`schedule` `set` takes exactly ONE of `message` or `prompt`, plus
optional `mode` (rejected with `message` — no turn runs, so no mode
applies; slugified like `/mode`, so display-name spellings land). The
docstring teaches the choice as a decision rule: message when the
wording is known (exact, instant, free), prompt only when the fired
turn must think or act, mode to pick the toolbox for that work. `list`
shows the kind, the mode, and the both-set warning.

## Relation to ADR 027's rejected "synthetic chat message"

ADR 027 rejected firing an event by having the bot post the *prompt*
into the chat and answer itself — a model turn dressed up as
conversation, noisy and fighting the echo suppressor. A simple event
is a different thing: a plain harness post of a canned string with no
model involvement at all, the same shape as WP33's reaction outcomes.
The rejection stands; this is not that.

## Rejected alternatives

- **Mode as an objects relation** (the `gc_default_mode` pattern): a
  picker in the Anytype UI, but id→name resolution has to happen in a
  layer that cannot see the registry, dangling links need their own
  error path, and archived-and-recreated modes orphan the link. Text
  matches how the system already persists "a mode, at rest".
- **Inert on both-set**: silently eats a reminder; visible only if
  someone runs `list`.
- **Temporary switch-and-restore of the chat's mode** (fire `/mode X`,
  run, `/mode` back): race-prone against concurrent turns, and a crash
  mid-fire leaves the chat silently switched. The per-turn pin cannot
  leak by construction.
- **A `kind` select property** ("Simple" | "LLM turn"): a third field
  to keep consistent with the two content fields; which-field-is-filled
  already discriminates, and the message-wins rule covers the one
  ambiguous state.

## Consequences

- Simple reminders are exact, instant, and free; the model's next turn
  still knows they went out (conversation memory hook).
- Prompt events get the right toolbox: a newsletter event can name a
  web-search mode while the chat stays in its everyday mode.
- Pre-existing prompt events change behavior (space default, not
  ambient) — documented above, deliberate.
- The bare MCP server can create all of this but still fires nothing
  (ADR 027's consequence stands); it validates mode names only at fire
  time, since it has no registry to check against at set time.
- A simple event's fire leaves no intent node and no turn-log records;
  if that ever matters, the bot's log line and `gc_last_fired` locate
  the fire.
