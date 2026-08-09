# ADR 045: Meta-inspection privilege + the Space Setup starter mode

Date: 2026-07-22
Status: accepted (amends ADR 015's "the LLM's traversal never sees mode
objects" — it still holds for every ordinary mode, but a mode carrying
the new privilege sees and writes them; sharpens the ADR 035 starter
corpus with a new marked default)

## Context

A fresh space seeds starter Activity Modes (ADR 035) and everything
after that is on the human: authoring further modes in the Anytype UI,
learning the `gc_mode_*` fields from the Example Mode explainer, wiring
types and rules by hand. The assistant — the thing that is good at
drafting prompts and knows every mode option — could not help, because
Activity Mode objects are infra (`Role.MODE`): hidden from every catalog
and traversal surface the LLM sees.

Two facts sharpened the design:

1. **The hiding was discoverability, not enforcement.** Nothing on the
   write path checked `INFRA_ROLES`: any mutating mode that *guessed*
   `type="Activity Mode"` could create or edit a mode object, and the
   `query` tool's documented infra escape hatch (an explicit infra type
   filter empties `exclude_roles`) already listed mode objects — goal
   bodies included — to any mode. Visibility needed to be *granted*
   somewhere and *enforced* everywhere else.
2. **There was no per-mode capability mechanism beyond `mutating`.**
   The tool binding is derived solely from that one flag (ADR 007/015);
   tool signatures cannot carry a privilege because
   `derive_schema` turns every parameter after `services` into a
   model-facing schema property.

## Decision

**One new ModeSpec capability, `meta_inspection`** (the
`gc_mode_meta_inspection` checkbox; parsed like `mutating` through
`spec_from_mapping`, stored/seeded/evaluated through the same seams).
The vocabulary of what it admits is a role set, not a boolean, at every
seam below — today it grants `{Role.MODE}`, nothing else.

**Threading: the privilege rides `Services`, set per call at the invoke
seam.** `Services.visible_infra_roles: frozenset[Role]` (default empty)
is written by `modes.invoke` on every dispatch — `{Role.MODE}` when the
active spec has `meta_inspection`, empty otherwise — so a `/mode` switch
revokes the surface with the mode, and nothing static ever holds the
privilege. Turns serialize per space under the route lock, so the
mutation cannot interleave. The bare MCP server never passes through
`modes.invoke` and therefore never holds the privilege.

**Visibility (read), scoped to `Role.MODE`:**

* `query`: `exclude_roles = INFRA_ROLES - visible_infra_roles`; the
  infra type-filter hatch **closes for Role.MODE** — an unprivileged
  `query(type="Activity Mode")` now raises an actionable refusal naming
  the Space Setup mode (this deliberately removes the pre-045 leak).
  Other infra roles keep their documented hatch.
* `find_node` / name resolution: `GraphIndex.find_by_name`/`resolve`
  gain `include_roles`; the tool layer passes the privilege, so mode
  objects resolve by name only for privileged calls.
* `get_node`: reads an infra target by id as before; the neighbor
  filter re-admits `visible_roles`.
* Catalogs: `GraphRepository.known_node_types(include_roles)` /
  `field_catalog(include_roles)` (port change, both backends + the
  registry) — the Activity Mode type and its properties appear only
  with the privilege. `explore` needs nothing (modes are reachability-
  hidden); the overview/statistics keep excluding MODE (noise).

**Reflection:** `gc_mode_*`/`gc_capture_*` join `GC_REFLECTED_FIELD_KEYS`
so a privileged `get_node` shows a mode object's config and the
`properties` dict writes it. The key/format table is domain-homed
(`activity.MODE_CONFIG_FIELDS`, aliased by the adapter `mapping` — the
scheduling/rules/attribution pattern) and the in-memory fake seeds it
into its catalog for bootstrap parity. These keys stay out of the
generic property suggestions (`_raise_unknown_field` filters `gc_`).

**Enforcement (new): the infra-write guard.**
`schema.validate_infra_write(role, type_name, admitted, known)` — the
rule in exactly one place — raises the new `InfraWriteDenied` (message
names the meta-inspection escape hatch and the schedule/automation
tools) unless the caller's `admitted` set covers the role.
`NodeWriter.create_node`/`update_node` take `admitted_infra_roles` and
the write tools pass `services.visible_infra_roles`. The dedicated infra
writers (scheduler, rule engine, recorders, session persister, seeder)
write through the repository directly and never meet the guard.
Consequence: the bare MCP server *loses* its accidental pre-045 ability
to write infra objects — deliberate defense-in-depth.

**The Space Setup starter mode** (`[modes.space_setup]`, all three seed
corpora; `mutating = true`, `meta_inspection = true`, marked
`default = true`, replacing world_modeling/organizing as the marked
default). Its goal prompt: interview the user about their use case, then
author a tailored Activity Mode via `create_node` (the prompt enumerates
every `gc_mode_*`/capture option with allowed values), propose object
types through the ADR 041 schema flow, suggest automation rules and
scheduled events, and hand off — naming the two human-only gestures
(`/mode <name>`; the Space Context "Default mode" link). Setting
`gc_default_mode` stays HUMAN-ONLY: the relation remains on
`SYSTEM_RELATION_DENYLIST` and no privilege pierces it.

**Rollout to live spaces:** the seeder stays seed-once (ADR 035), so
already-seeded spaces get the mode from the one-time
`scripts/seed_space_setup_mode.py` — per binding: `ensure_schema` (mints
the new checkbox), skip if any mode slugifies to `space_setup`, else
mint via the seeder's (now public) `create_payload`. It never touches
`gc_default_mode`; existing spaces keep their defaults, only FRESH
spaces start in Space Setup.

## Consequences

* A setup conversation can now build the space: mode objects authored by
  the model land as real, immediately loadable Activity Mode objects
  (the ADR 044 change tick or `/mode` picks them up — the prompt tells
  the model to hand the user `/mode <slug>`, which is the unconditional
  reload if one auto-refresh tick misses the store's search settle).
* Every unprivileged surface is strictly tighter than before: the query
  hatch refusal replaces a silent leak, and guessed infra writes fail
  with a self-correcting error. Three eval cases that relied on
  unprivileged mode authoring were repinned to `space_setup`.
* Memory-backend caveat: a mode object created via `create_node` in the
  memory backend (dev/CLI, eval worlds) becomes a graph node but not a
  *loadable* mode — `InMemoryModeStore` is a static payload list. Evals
  assert the create, not a subsequent switch.
* The privilege vocabulary is a role set; a future surface (e.g. letting
  a diagnostics mode read Scheduled Events) is one more role in the
  admitted set, not a new mechanism.
