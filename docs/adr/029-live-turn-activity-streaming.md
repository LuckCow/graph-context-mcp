# ADR 029: Live turn activity streaming in the Anytype chat

Date: 2026-07-14
Status: accepted (amended 2026-07-15: the detail level is a property of
the activity MODE — profile spec, `GC_MODES_FILE`, or the in-space
Activity Mode object — not a `/mode detail=` session setting; picking a
mode picks its verbosity. Amended 2026-07-17: `close` DELETES the
activity message instead of collapsing it into a done-summary — the
trace is live scaffolding, not chat history; the summary edit survives
only as the degrade path when the delete fails)

## Context

Since WP14 (ADR 019) the Anytype chat bot posts a static `Processing…`
placeholder when a turn starts and later edits it into the first chunk of
the reply. A turn can run up to 16 driver decisions with tool calls in
between; the user stares at `Processing…` the whole time and learns
nothing about what the assistant is doing. Dogfooding wanted live
progress — what tool just ran, how far along the turn is — with the
verbosity under the user's control.

Two facts shape the design:

- **Anytype has no write-side streaming API.** The chat's
  `messages/stream` endpoint is read-only SSE. But every PATCH to a
  message reaches watching clients instantly as a `message_updated`
  event over that same subscription — **edit-in-place IS the streaming
  mechanism**.
- **The API allows a burst of 60 requests, then ~1 request/second
  sustained**, shared with the turn's own graph writes. Per-event posts
  would flood it; edits must coalesce.

The pipeline had no mid-turn seam: `handle_message` returns all
`ReplyEvent`s in one batch, and the only per-event taps are the
`TurnLog` diary calls (ADR 025's JSONL, consumed after the fact).

## Decision

**One activity message, edited in place, deleted at the end.** The
`Processing…` placeholder still posts the moment the turn starts (before
the route lock — a queued message must show progress). When the turn
begins streaming, the sink *claims* the placeholder
(`TurnReply.claim_placeholder`) as its activity message and PATCHes the
rendered activity into it as events arrive. The final reply then posts
as a **fresh message** (a claimed `TurnReply` naturally falls into its
plain-send branch), and `close` DELETES the activity message: the trace
is live scaffolding, not chat history — the turn log keeps the full
record — and the reply alone remains. (2026-07-17 amendment; originally
the message collapsed into a compact done-summary like `✓ 4 tool calls ·
3 decisions`, which dogfooding read as clutter. That summary edit
survives only as the degrade path when the delete fails, so a live
"working…" text never strands.)

**A narrow per-turn observer, not a TurnLog sibling.** `handle_message`
gains an optional `observer: TurnObserver | None` parameter — a
three-method async protocol (`turn_started`, `decision`, `tool_result`)
fired beside the existing `turn_log.llm_turn` / `turn_log.tool_result`
calls. It is a *parameter*, not a field, because its identity is
per-turn (bound to one chat's message), unlike the process-lifetime
diary. `TurnLog` stays sync and untouched; forcing one abstraction over
both would either churn every diary call site or force fire-and-forget
scheduling from sync code. `None` — every other caller: CLI, Discord,
MCP, scheduled turns — costs nothing and changes nothing. Observers must
not raise; delivery failures degrade internally (the TurnLog posture).
Command turns (`/mode`, `/clear`) return before `turn_started`, so they
never stream. Streaming granularity is per-decision/per-tool-result, not
per-token: both drivers return whole `LLMTurn`s to the pipeline
(ADR 007), and that granularity is provider-independent.

**The mode owns the setting; the renderer owns its meaning.** The
detail level — `off | minimal | tools | full` — is a `ModeSpec`
property (`activity_detail`, default `minimal`, validated in
`ModeSpec.__post_init__`): selecting a mode selects its verbosity, so a
deployment tunes streaming the same way it tunes everything else about
a mode. All three config sources can set it — a profile's built-in
specs, a `GC_MODES_FILE` table (`activity_detail = "tools"`), and the
space's own Activity Mode objects via a new `gc_mode_activity_detail`
**select** property whose options (Off/Minimal/Tools/Full, Title-Case
derivations of the canonical levels) bootstrap pre-seeds — the human
picks from a dropdown instead of typing the enum. It is minted inline on
the type like the other `gc_mode_*` / `gc_capture_*` fields; on spaces
whose type predates the field, `ensure_schema` retrofits it via the
type-update endpoint (quirk A11, live-verified by
`scripts/spike_type_update.py`: the update's `properties` list is
wholesale, so the full fetched list rides along with the addition), and
a variant minted under an older format is healed by delete + re-create
(quirk A12: formats are immutable — PATCH silently keeps the old one).
An unpicked select means "not set" → default; values are case/padding
normalized on read (so the Title-Case options match the lowercase
levels), and an unknown value fails at load time naming the object or
TOML table, like every other spec error. Bare `/mode` reports the
active mode's level. The pipeline stores nothing: it always
reports all events and hands `spec.activity_detail` to `turn_started`;
what each level *shows* is interpreted in exactly one place, the
renderer (`orchestrator/turn_activity.py` `ActivityLog`). The transport
never reads the setting at all — the sink learns it from
`turn_started`.

- `off` — the sink stays inert; the pre-WP19 placeholder lifecycle,
  bit-for-bit (pinned by `TestProcessingPlaceholder`).
- `minimal` — decision counter + deduped tool-name tally.
- `tools` — one line per call with an argument summary and ✓/✗/… mark.
- `full` — plus thinking snippets, interim model text, result excerpts.
  Under the 2000-char message budget, excerpts drop from all but the
  newest decision first, then the oldest decisions collapse into one
  `… n earlier steps` line; the header and newest decision always
  survive.

**Leading-edge coalescing, no timers.** The sink edits at most once per
`ACTIVITY_EDIT_SECONDS` (2s): an event inside the window folds silently
into the log; the next event past it flushes everything accumulated
(the unconditional closing delete supersedes anything still unflushed).
Worst case stays under half
the sustained request budget. Rejected: a trailing-edge debounce task
(timers and cancellation in a pure module, nondeterministic tests) and a
client-side throttle in the chat client (it would delay the reply
itself, and the client deliberately has none).

**Wiring.** The transport's `run_turn` takes an optional
`ActivityObserver` (the pipeline protocol plus `close`), forwards it as
the observer, and closes it after the reply is delivered — "clean up
after the reply posts" is transport sequencing the pipeline cannot see.
The composition root builds the concrete `ChatActivity` beside each
`TurnReply` and closes it with `ok=False` on the error paths, so a
crashed turn posts its error fresh and still deletes its activity
message. Echo suppression is free: the activity message *is* the
placeholder, whose id `open()` already recorded; claiming transfers
ownership, not identity. Scheduled turns (ADR 027) keep posting nothing
until the reply is ready — `run_scheduled` takes no sink.

## Consequences

- Every streamed turn leaves ONE bot message — the reply — same as
  detail `off`; the trace exists only while the turn runs. The chat is
  not an audit surface: the full trace lives in the turn log. The
  notification cost is unchanged either way (two posts per streamed
  turn: the placeholder and the fresh reply; the chat API offers no
  silent-post flag, and deleting a message cannot retract a
  notification already shown).
- The `ok`/error reading of a tool result lives with the `ERROR: `
  prefix rule in `interface/tools.py` (`is_error_result`), not re-spelled
  in the pipeline.
- A future transport (Discord) can reuse `TurnObserver` with its own
  sink; the renderer is Anytype-flavored only in its message-size
  budget.
- WP14's `TestProcessingPlaceholder` is re-documented as the no-activity
  contract; the streaming lifecycle is pinned by `TestActivityStreaming`
  and `tests/orchestrator/test_turn_activity.py`.
