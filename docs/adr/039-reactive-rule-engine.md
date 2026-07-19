# ADR 039: Reactive rule engine — automations as graph nodes

Date: 2026-07-19
Status: accepted

## Context

Scheduled Events (ADR 027) let the assistant initiate on *time*. The
next dogfooding ask is initiating on *state*: "when I tick a task's Done
box, stamp the completion date", "these objects share one Default
checkbox — checking one should uncheck the others". The user wants
reactive automations they configure themselves, at runtime, from the
Anytype UI — not code changes.

The original design sketch proposed a standalone sidecar speaking gRPC
to anytype-heart (its `ObjectSearchSubscribe` stream is the only real
object-change push Anytype has) with a SQLite state store. Both halves
were re-decided against this repo's existing machinery:

- **gRPC was already evaluated and shelved**
  (`docs/spikes/grpc-event-backbone.md`): the REST local API has no
  object-change push, but the unthrottled headless sidecar makes a
  modified-since `POST /search` poll near-instant and free, while gRPC
  needs vendored proto stubs, new deps through the egress firewall, and
  has unverified session auth. Polling + diffing the derived index is
  the change-detection mechanism; the gRPC backbone remains a possible
  future *accelerator*, not a prerequisite.
- **A separate state store breaks ADR 001/002's shape** — Anytype is
  the surface, projections are derived and rebuildable, bookkeeping
  lives on nodes. The transition baseline is in-memory (below); the
  durable bookkeeping (`gc_rule_last_fired`, `gc_rule_last_error`)
  lives on the rule object where a human can see and repair it.

## Decision

**A rule is a node.** A new infra type `gc_rule` (display "Automation
Rule", role `AutomationRule`, member of `INFRA_ROLES`) carrying nine
minted properties (keys `gc_` for wire stability, minted under human
display names, attached inline so the editor shows the fields; all nine
join `GC_REFLECTED_FIELD_KEYS`, the ADR 027-amendment lesson):

- `gc_rule_target_type` ("Rule target type", text) — which object type
  the rule watches, by display name or key, case-insensitive.
- `gc_rule_watch_property` ("Rule watch property", text) — the scalar
  property whose *transitions* trigger the rule, by display name or
  key.
- `gc_rule_condition` ("Rule condition", select) — `Changed to true`,
  `Changed to false`, or `Changed`.
- `gc_rule_action` ("Rule action", select) — `Set property to now`,
  `Set property value`, or `Uncheck others of type`.
- `gc_rule_action_property` ("Rule action property", text) — the
  property the action writes (defaults to the watch property for
  uncheck-others).
- `gc_rule_action_value` ("Rule action value", text) — the value
  `Set property value` writes.
- `gc_rule_status` ("Rule status", select) — `Active` / `Paused` /
  `Error`. **Lenient like ADR 027's status**: empty and unknown values
  read as active; only an explicit off-word (`paused`, `disabled`,
  `off`, `cancelled`) pauses. `Error` is **engine-owned and
  self-healing**: the engine revalidates every tick, writes `Error` +
  `gc_rule_last_error` when a rule fails to parse or resolve (only when
  the stored values differ — no per-tick write spam), and flips it back
  to Active with the error cleared once the config parses again. The
  engine never writes `Paused` — that word is the human's.
- `gc_rule_last_fired` ("Rule last fired", text) — observability stamp,
  engine-owned.
- `gc_rule_last_error` ("Rule last error", text) — the most recent
  validation/action failure, truncated, engine-owned.

A rule whose target type AND watch property are both empty is an
**unconfigured template**: skipped silently, never an error — which is
also what keeps the seeded "Example Automation Rule" explainer (the
ADR 027 pattern, minted with the type) quiet.

**Transitions, not states — via an in-memory baseline.** The engine
(one per space) keeps a private snapshot `{object id → {watched
property key → last-seen value}}`, restricted to the (type, property)
pairs enabled rules watch. Each tick diffs the snapshot against the
current index, fires matching rules, then **re-baselines from the
post-action index state**. Consequences, all deliberate:

- *Nothing fires on restart or sync replay*: the first tick only
  records the baseline. A transition made while the engine was down is
  silently absorbed — the human already saw the checkbox flip; replaying
  states as transitions is exactly the re-fire bug the design forbids.
  The trade-off (a task completed during downtime gets no
  `completionDate` stamp) is accepted and self-heals on the next real
  change.
- *An engine write can never trigger a rule* — the loop-prevention
  property, exact by construction: the whole tick runs under the
  route's turn lock, repository writes are write-through, so the
  engine's own writes are folded into the baseline before the next
  diff. Rules therefore **do not cascade**, ever (a rule watching a
  property another rule writes sees nothing; rule-load logs an
  informational note when configs overlap that way).
- *At-most-once per (rule, object, transition)*: the baseline advances
  whether or not the action write succeeds. A failed action lands in
  `gc_rule_last_error` and is **not retried** — retrying `uncheck
  others` against evolved state would be wrong, and a crash-looping
  action is worse than a missed one (the ADR 027 argument).
- Unknown object ids and newly watched keys baseline silently (covers
  startup, newly created objects — a Task *born* with Done ticked never
  transitioned — and newly enabled rules). Objects that vanish from the
  index are pruned without firing.
- Absence is false: the adapter drops unticked checkboxes from
  `Node.fields`, so `changed to true` means `prev ≠ "true" and
  curr = "true"` with `""` ≡ absent (the fake, which stores an explicit
  `"false"`, agrees through truthiness comparison).

**Matching is pure domain** (`domain/rules.py`: vocabulary constants,
`parse_rule_fields` → frozen `RuleConfig`, `condition_met` — no clock,
no I/O; errors are prompts echoing the allowed words). The service
(`application/rule_engine.py`, `RuleEngine`) mirrors `Scheduler`:
injected `local_clock(GC_TIMEZONE)` now, rules read straight off
`repository.graph` by role (no new port — the scheduler precedent),
writes through `update_node` with the full merged fields map. Property
resolution goes through `repository.field_catalog()` (by key or display
name, case-insensitive); a type the catalog doesn't know degrades to a
literal, case-insensitive fields-key match rather than erroring, so the
memory backend works without a space schema.

**Three built-in actions, no user scripts** (a Python snippet in a text
relation is arbitrary code execution for anyone with vault write
access; a script layer would need sandboxing we don't want to own):

1. `Set property to now` — stamps the action property with the local
   wall clock (same clock and stamp shape as the scheduler).
2. `Set property value` — writes the configured value; type-checked at
   validation time against the target property's format where known.
3. `Uncheck others of type` — the exclusive-default recipe: fires on
   `changed to true` (the condition field may be left empty or must
   agree), then writes `false` to the property on every *other* object
   of the type currently true. Naturally idempotent; two objects
   flipping true in the same tick resolve in node-id order,
   last-writer-wins.

**Watching is a fourth watcher in the Anytype bot** (`_watch_rules`,
`GC_RULE_TICK_SECONDS`, default 5, `off` disables). Unlike
`_watch_schedule` — a pure read riding `_watch_graph`'s 60s resync —
the rule tick runs its **own** `resync_graph()` first, under the route
lock: a checkbox answered a minute later reads as broken, and the
modified-since search is a few localhost calls against the unthrottled
sidecar. (Side effect: the shared index stays fresher for turns.) Every
bound space gets an engine; per-chat sessions are irrelevant — rules
belong to the space, so `Services` shares one `RuleEngine` by
reference, like the scheduler.

Scope guards: the watchable surface is scalar `Node.fields` properties
only (not name, summary, body, or relations); no LLM tool in v1 —
authoring is the Anytype UI (infra role keeps rules out of traversal;
`query type="AutomationRule"` is the escape hatch); no cross-space
rules. The MCP server and CLI have no tick loop, so rules are inert
there, like scheduled events.

## Consequences

- Both canonical automations work end-to-end with zero deployment
  config: create an Automation Rule object in the UI, and within ~5s of
  a matching transition the engine writes the effect and stamps "Rule
  last fired". Broken configs surface as `Error` + "Rule last error"
  *on the rule object* and heal themselves when fixed.
- A "Rules" collection view in Anytype is a free management dashboard.
- Text-property watches with `Changed` can fire on a half-typed value
  (the UI saves continuously); checkbox/select watches are atomic. The
  explainer documents the caveat.
- `Set property to now` is format-aware (R2, live-probed 2026-07-19):
  a native date property rejects naive timestamps and accepts RFC 3339
  only WITH a timezone — which the naive-local clock convention cannot
  honestly supply — or a bare date. Date targets therefore get the bare
  **local date** (`YYYY-MM-DD`); text targets carry the full
  `YYYY-MM-DD HH:MM:SS` stamp. Want time-of-day? Use a text property.
- A 5s default tick re-runs `resync()` (which also reloads the
  registry) — acceptable against the sidecar, tunable via the knob; a
  registry-skipping fast resync is possible follow-up work.

## Alternatives considered

- **gRPC `ObjectSearchSubscribe` push** — see Context; remains the
  future accelerator path if polling ever becomes the bottleneck.
- **Persisted transition state (SQLite/JSON)** — would catch downtime
  transitions, at the cost of a second store of record that can drift
  from the space and replay stale state as fresh transitions. Rejected;
  in-memory + baseline-on-start is the semantics we actually want.
- **Free-form Python in a `script` relation** — code execution for
  anyone with vault write access; sandboxing is a project of its own.
  Built-ins cover the motivating cases; a script layer can be a later
  WP if ever.
- **An `enabled` checkbox** (the original sketch) — an unticked
  checkbox is indistinguishable from an absent one in `Node.fields`,
  and three states are needed anyway (`Error`). One lenient status
  select matches ADR 027 and keeps the surface consistent.
- **Riding `_watch_graph` instead of own resync** — couples reaction
  latency to a knob tuned for background freshness (60s); a reactive
  feature that reacts in a minute reads as broken.
