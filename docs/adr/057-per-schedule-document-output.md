# ADR 057: Scheduled Events — per-schedule document output

Date: 2026-08-07
Status: accepted (amends ADR 027/055; builds on ADR 048)

## Context

A recurring "compile the weekly AI newsletter" prompt event fires,
searches, and posts a ~5KB newsletter straight into the chat — every
week. The content is long-form, worth keeping, and worth editing; the
chat is the wrong home for it. ADR 048 already solved exactly this
shape at MODE level: a document mode's model maintains the manuscript
in a node and the reply carries a short summary plus the object card.
And ADR 055 made scheduled turns run through the identical pipeline
path as interactive ones — so pinning an event to a document-typed
mode produces node + card today.

But the granularity is wrong. Document output is a property of the
*job*, not the mode: making one newsletter land in a node currently
means authoring a whole Activity Mode with `gc_mode_document_type` set
and pinning the schedule to it — mode sprawl for a one-knob
difference, and the mode's interactive use inherits the document
discipline whether wanted or not.

## Decision

### One new property: `gc_schedule_document_type`

A prompt event may name the node TYPE its fired turn's output lands in
(display "Schedule document type"; joins `SCHEDULED_PROPERTIES`, so
mint, retrofit, and `GC_REFLECTED_FIELD_KEYS` reflection all follow
from the existing derivations; the seeded explainer documents it).
Like `gc_schedule_mode` it is deliberately lenient TEXT, not a select
— type names are live space data, and a typo degrades at fire time as
a `create_node` error the model self-corrects in-turn. There is no
set-time type vocabulary check, matching mode's posture on the bare
MCP server.

Validation mirrors mode's: the `schedule` tool's `set` rejects
`document_type` with `message` (a verbatim post runs no turn, so
nothing could write a document), and `Scheduler.tick`'s message-wins
branch leaves it empty for the same reason. The tool does NOT slugify
it — it is a type name ("Report"), not a mode slug.

### Fire-time overlay, turn-local, after mode-pin resolution

`DueEvent` carries the type; `run_scheduled` passes it to
`handle_message(document_type=…)`. The pipeline applies it AFTER the
ADR 055 mode pin resolves, as a `dataclasses.replace(spec,
document_type=…, capture=None)` on this one turn's spec — never
persisted, exactly like the mode pin. Everything downstream is ADR
048 unchanged: `goal_for` appends `DOCUMENT_GUIDANCE`, the attach
stamping cards the turn's touched nodes of that type on the reply,
`hide_node_cards` never filters the explicit attach.

Two rules fall out of `ModeSpec`'s own validation (`replace` re-runs
`__post_init__`):

- **A read-only mode degrades loudly.** The overlay requires the
  effective mode be `mutating`; otherwise the pipeline logs a warning
  and runs the turn WITHOUT the override. Mutation tools are never
  granted implicitly — the event author picks a mode whose tools fit,
  same as for web search.
- **`capture=None` for the overlaid turn.** The document/capture
  mutual exclusion holds; a capture mode's reply is not additionally
  copied into an artifact node — the document IS the artifact.

The per-schedule type beats a pinned document mode's own
`document_type`: the schedule states the job's output, the mode
supplies tools and voice.

## Rejected alternatives

- **A dedicated document mode per schedule.** Works today, but mode
  sprawl for a one-knob difference, and it taxes the mode's
  interactive use with the document discipline.
- **Harness-side reply rewriting** (post the reply into a node the
  transport creates). ADR 048 already rejected rewriting: the model
  curates the document — references, revisions, title — and the
  harness cannot.
- **A select property.** ADR 055's reasoning verbatim: type names are
  live space data; pre-seeded options go stale.

## Consequences

- Typos in the type name surface at fire time, not set time — the
  model's `create_node` error names the space's actual types and the
  turn self-corrects or reports.
- The overlaid turn's goal differs from the mode's, so the turn-log
  prompt fingerprint re-logs when scheduled and conversational turns
  alternate — cosmetic, identical to mode pins.
- The bare MCP server stores the property but never fires events
  (ADR 055's posture, unchanged).
- Discord still has no scheduled path; the feature is Anytype-only
  like the rest of ADR 027.
