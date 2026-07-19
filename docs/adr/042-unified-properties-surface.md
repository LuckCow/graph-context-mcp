# ADR 042: One `properties` surface — scoped creation, unified relations+fields, built-in rule watchables

Date: 2026-07-19
Status: accepted (amends ADR 023's fields opt-in and ADR 006's
`create_missing_relations` into one declaration surface; supersedes the
incoming-link half of the WP1 composite-create choreography)

## Context

Two dogfooding turns exposed the property-creation surface as the
system's most fragmented seam:

* **Turn `ba3ab5c05a28`** — "create a rule that fires whenever Gerald's
  last modified date changes." The model guessed `watch_property=
  modified_at`, was told `type 'Character' has no property
  'modified_at'`, and punted to the user. Underneath: the rule engine
  resolves watches against TYPE-attached properties, but the model's
  on-the-fly minting (`create_missing_fields`, ADR 023) creates only
  *unattached space-level* properties — invisible to rules — and the
  one type-attaching path is the differently-shaped `schema` tool
  (ADR 041). The model had no vocabulary to even reason about the
  distinction. And the underlying wish — fire on ANY edit — was
  impossible: the store's own modified stamp was not watchable at all,
  though every `Node` already carries it (`modified_at`, ADR 016).
* **Turn `de38192f56dc`** — after a human detached "Shift Active" from
  Character in the UI, the model retried
  `update_node(fields={"shift_active": true},
  create_missing_fields={...})` four times, each an opaque "internal
  error". Root cause: the JSON **boolean** `true` reached
  `parse_checkbox`, which calls `.strip()` — an `AttributeError`
  swallowed by the `guarded` boundary. (The mint itself succeeded, with
  the raw key as its display name — a second paper cut.) A declared
  property that already exists must be a clean reuse, and JSON-native
  values must coerce, never crash.

Counting surfaces: `fields`, `links`/`add_links`,
`create_missing_relations`, `create_missing_fields`, and the `schema`
tool — five places where "make/set a property" lived, for a store in
which they are all just properties (a relation is the `objects` format,
ADR 003/006).

A live spike (2026-07-19, GC-E2E) settled the remaining unknowns:

* **A14** (new quirk): `POST /properties` with an existing key 400s
  `property key "…" already exists`; a duplicate NAME on a fresh key is
  fine; DELETE frees the key. The server normalises requested keys
  (letter/digit boundaries gain underscores), so the response key is
  authoritative.
* **A11 amendment**: type POST/PATCH entries attach not-yet-minted
  properties of any format — `objects` included — but an already-minted
  `objects` property attaches only when the entry carries its `id`
  (key-only 400s "already exists"); the id form works for every format,
  so reuse entries always include it.

## Decision

**One `properties` dict on `create_node`/`update_node`.** Scalars and
relations in a single map, discriminated by what the space says the key
IS: a key naming an existing `objects` relation takes a node id/name
(or a list) and becomes link(s); everything else is a scalar value.
`fields`, `links`, `add_links`, `create_missing_relations`, and
`create_missing_fields` are retired — kept as implementation-only dead
parameters that raise a self-correcting redirect (replayed transcripts
and stale habits must not hit an opaque error). On update, relation
entries **APPEND** (a wholesale replace would silently destroy targets
the model never saw — the A4 clobber ADR 009 exists to prevent);
`remove_links` stays for removals, and reassignment is documented as
add + remove.

**Creation stays explicitly declared, now with scope.**
`create_missing_properties={key: {"format": F, "scope":
"instance"|"type", "name": <optional display name>}}` (string shorthand
= instance scope). Formats are `CREATABLE_FORMATS` = the ADR 023
scalars + `objects` — retiring `create_missing_relations` folds relation
minting into the same declaration. Scope semantics:

* `instance` — today's behaviour: a space-level mint
  (`POST /properties`), value on the one object, attached to no type.
* `type` — the write proceeds IMMEDIATELY with the same space-level
  mint + value, AND drafts an EXTEND_TYPE schema proposal into the
  session ledger (`NodeWriter` → `SchemaProposals`), riding ADR 041's
  drain/confirm/👍 flow unchanged. The human gate on ALL type changes
  stays: only the reaction attaches. `add_type_properties`' reuse
  semantics make the confirm naturally attach the already-minted
  property, retry-safely. A drafting failure (ledger cap, conflict)
  degrades to a warning — a landed write is never unwound.

Docstrings teach the heuristic: recurring attribute of the kind → type
(required for rule-watchability); one-off fact about this object →
instance.

**`PropertyDraft` accepts `objects`** (its ADR 041 rejection is
lifted): a proposal may create or attach a relation to a type. The
no-scalar-shadow rule survives sharpened: a *scalar* draft naming an
existing relation is a format conflict (A12); an `objects` draft
naming one is a reuse-attach (by id, per the A11 amendment).

**Incoming links retire** (`LinkSpec.outgoing` is gone). An edge is an
entry in an `objects` property on its SOURCE (ADR 003); a properties
dict can only describe its own node's relations, and the reverse edge
is the other node's own write (two-call pattern in the docs). This
deletes the composite-create's patch-other-objects choreography and its
rollback restore; the outgoing-failure rollback (archive the node)
survives.

**Robustness (the de38192f56dc fixes).** The tool boundary coerces
JSON-native scalar values (bool → "true"/"false", numbers via
`render_number`, string lists for multi_select), rejecting the rest
loudly by property name. The adapter maps the A14 duplicate-key 400 to
a typed conflict naming the way out; a declared key matching an
existing same-format property is a silent reuse, a format mismatch a
loud A12 conflict — checked in BOTH repositories (fakes are contracts).
Minted properties derive a human display name from the key
(`shift_active` → "Shift Active"); the declaration's `name` overrides.

**Built-in rule watchable.** `modified_at` (aliases: "last modified
date", "modified date", "modified", "last modified"; vocabulary in
`domain/rules.py`) resolves as a watch on ANY type — reading
`Node.modified_at`, the store-clock stamp — when the type catalog does
not claim the identifier (a space's own "Modified date" property wins).
Condition `changed` only; never an action property. Both backends now
keep the stamp truthful on every write: the in-memory fake stamps a
deterministic monotonic clock, and the Anytype adapter folds link-write
PATCH responses back into the index (previously link writes left the
stamp stale under self-write suppression). The engine's no-cascade
guarantee carries over by construction — its own writes bump the stamp
*before* the tick-end baseline rebuild absorbs them.

## Consequences

* The ba3ab5c05a28 turn works two ways now: `watch_property="last
  modified date"` binds directly, and the fallback ("create a checkbox
  and watch it") is one write with `scope: "type"` plus one 👍.
* The de38192f56dc payload — boolean value, already-existing declared
  key — succeeds cleanly.
* The MCP wrappers advertise only the new surface; the tool
  implementations keep the retired params as redirects
  (`tests/interface/test_server_wrappers.py` pins both halves).
* `WriteOutcome` (node + drafted proposals + warnings) replaces the
  bare `Node` return of `NodeWriter`; the pipeline's existing
  `drain_drafted` posts writer-originated confirms with no changes.
* A latent watermark bug surfaced and is fixed: composite creates
  tracked the POST response after the relation PATCH, overwriting the
  newer seen-stamp — resync would have flagged our own write as an
  outside edit. `_track_watermark` now keeps the max stamp per object.
* Read-side reflection is untouched: scalars reflect into `fields`,
  `objects` properties reflect as edges (ADR 012). Only the write-side
  vocabulary unified.
* Eval scripted cases and the demo scripts moved to the new surface;
  the profile goldens were regenerated as the prompt-review artifact.
