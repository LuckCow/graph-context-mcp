# ADR 008: Provenance is a harness responsibility (intent nodes + automatic capture)

**Status:** Accepted (2026-07-02); amended 2026-07-04 — the `record_prose` tool was removed entirely rather than kept as the harness-less voluntary path (pre-deployment, no frozen surface); capture is exclusively the harness's job — depends on ADR 007; amends WP3's
`record_prose` design

## Context

Every graph mutation has an origin — a user prompt and the model's working
steps — but that provenance is visible only to the harness (ADR 007). Today
it is captured only if the model volunteers it (`record_prose` and its
`llm_input`/`llm_output` parameters), which dogfooding shows is unreliable.
And the Anytype API exposes no version history, so "why does this node say
what it says?" is otherwise unanswerable.

## Decision

**The harness records provenance automatically; the model is never asked
to.**

- **Per-turn mutation journal.** Application writers report the node ids
  they create or modify to a `MutationJournal` observer — a no-op in the
  MCP server, a per-turn collector in the orchestrator. Writers already
  know what they touched, so the fact is recorded where it originates; no
  parsing of presenter output.
- **One intent node per mutating turn.** At turn end, if the journal is
  non-empty, the orchestrator creates a single `gc_intent` node in ONE
  call: body (write-once, per spike S6) = the verbatim user prompt plus a
  condensed tool-call trace (tool, condensed args, one-line outcome each),
  capped with an explicit `[truncated]` marker; edges labelled **`intent`**
  to every node created or modified in the turn, populated at creation
  (per spike S1). One write per turn — throttle-friendly. Read-only turns
  write nothing.
- **Attribution properties** (added 2026-07-02, multi-user direction). Each
  intent node carries `gc_user_id` (transport-scoped id of the human whose
  message drove the turn, e.g. `discord:8291…`, `slack:U04…`) and
  `gc_model` (the model id that executed it) as scalar properties —
  queryable, not buried in the body. This matters because in a shared space
  Anytype's own `creator`/`last_modified_by` show the *bot* identity for
  every orchestrator write; intent nodes are the only genuine attribution
  record.
- **The chain includes artifacts.** In authoring mode the harness also
  captures produced text automatically (entity-linked against `GraphIndex`
  names for `references` edges), and the turn's intent node links to that
  artifact too. Full chain: *user prompt → intent → artifact + touched
  nodes*.
- **Hidden by default, surfaced on request.** `gc_intent` carries an infra
  role, so it is hidden from `explore` exactly as Prose and SessionContext
  are; edges to infra-role nodes are additionally suppressed in
  `get_node`'s edge grouping (otherwise every node accumulates visible
  intent backlink noise). `get_node(include_provenance=N)` returns the N
  most recent intent nodes touching the node — the same pattern as
  `include_prose`.
- **Created-vs-modified detail lives in the body**, not in split edge
  labels; v1 records which fields were touched, not before/after diffs.
- **`record_prose` keeps its place on the MCP surface** — the voluntary
  capture path for harness-less clients — but its
  `llm_input`/`llm_output`/`model` parameters are retired: that provenance
  now lives on intent nodes.
- **Privacy and hygiene.** Prompt storage is governed by config (extending
  the `GC_STORE_LLM_INPUT` knob), and the whole subsystem is toggleable.
  Intent nodes are named scannably — `Intent: <first ~60 chars of prompt>
  — <timestamp>` — so the type's list view in Anytype reads as an activity
  log, not clutter.

## Consequences

- A queryable audit trail Anytype itself does not provide: from any node,
  back to the exact prompt and tool calls that shaped it.
- No port changes: intent nodes are ordinary nodes with ordinary edges, so
  the fake, contract suite, and live E2E cover them without new
  capabilities.
- Costs one extra write per mutating turn, well inside the ~1 req/s write
  budget.
- MCP-only usage produces no intent nodes; the audit trail is an
  orchestrator feature by construction.
- Deliberately deferred: multi-turn intent chains (a `follows` edge between
  intent nodes for "now continue"-style turns), before/after diffs, and
  retention/pruning of old intent nodes (left to the human — it is their
  space).
