# ADR 023: Story-node fields are native-only; gc_fields is infra-only

**Status:** Accepted (2026-07-10) — amends ADR 012's write-side routing

## Context

ADR 012 gave `fields` two write channels: keys matching an existing
reflectable property (by key or display name) wrote that property; every
other key silently fell through to the `gc_fields` JSON blob. In practice
the fall-through swallowed near-misses: a session wrote `due` on a Task
whose type already had a **"Due date"** property, and the value landed in
the blob — invisible in Anytype queries, Set views, and the type's
template properties. Two gaps compounded:

1. **The LLM never saw the property catalog.** Nothing listed "Task has
   Due date (date), Status (select)"; the model could only discover a
   property by reading a node that already carried a value.
2. **An unmatched key degraded silently** instead of erroring or minting
   a real property, so a typo'd key produced hidden data instead of a
   correction.

A live spike (2026-07-10, GC-E2E sidecar) confirmed a freshly created
**scalar** property is immediately usable in both `POST /objects` and
`PATCH` — unlike `objects`-format relations, there is **no settle
window** — and that `GET /types` returns each type's own `properties`
array (already read by `scripts/spike_template_props.py`).

## Decision

1. **Story-role writes are native-only.** Every `fields` key on a
   non-infra node must resolve to an existing reflectable scalar property
   (matching stays space-wide via `registry.field_property`). No
   `gc_fields` entry is written for story nodes at all.
2. **An unmatched key raises `UnknownFieldKey(ApprovalRequired)`** before
   any persistence, listing the requested type's own properties first
   (with select options, fetched lazily and memoized until the next
   registry rebuild), then the rest of the space's, then the
   `create_missing_fields` recipe — errors are prompts.
3. **New properties are an explicit, format-carrying opt-in.**
   `create_missing_fields={"key": "format"}` on `create_node`/`update_node`
   (formats: the nine reflected scalar formats, now domain vocabulary as
   `schema.FIELD_FORMATS`) creates the property via `POST /properties`
   and registers it for reuse — mirroring `create_missing_relations`.
   A declared key that matches an existing property is reused; the
   declaration is ignored. Well-formedness (format known, declared key
   present in `fields`) is validated once, in `NodeWriter`, via
   `schema.validate_field_declarations`.
4. **The catalog is taught, not guessed.** `GraphRepository.field_catalog()`
   exposes reflectable properties per type display name (from the per-type
   `properties` array `load_registry` now keeps on `TypeInfo`); properties
   no type claims — including ones this mechanism mints, since
   `POST /properties` does not attach to a type — surface under an
   `"(any type)"` bucket. `context action='overview'` renders the catalog;
   the tool docstrings direct the model to it.
5. **`gc_fields` survives for infrastructure only.** Infra-role writes
   (Prose/intent recorders' bookkeeping, session snapshots per ADR 021)
   keep the ADR 012 match-else-blob routing. The read-side merge in
   `to_node` is unchanged, so legacy blob data on story nodes stays
   visible (native wins on collision) — read-compat, no migration.

The in-memory fake takes an optional `field_catalog` constructor
parameter (`None` keeps the historical open fields — memory backend and
demos unchanged); with a catalog it enforces the same contract, certified
by `FieldCatalogContract` against both repositories.

## Consequences

- The "due vs Due date" failure mode is now a self-correcting error: the
  model reads the property list out of the error (or the overview) and
  resends with the real name.
- New scalar properties enter the space only when the model explicitly
  declares key + format, so views/templates see real, typed properties.
- Legacy blob keys on story nodes become bot-immutable (no blob writes);
  they remain visible via the read merge until a human promotes the key
  to a real property. Accepted: no migration script.
- Mock fidelity: type creation with inline `properties` now stores them
  with ids on the type object (live shape); the settle-window knob stays
  `objects`-only per the spike.
