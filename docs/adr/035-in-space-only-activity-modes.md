# ADR 035: In-space-only activity modes; the TOML becomes a seeder

Date: 2026-07-17
Status: accepted (supersedes ADR 015's three-source precedence; starts
the DomainProfile deprecation, completed by WP27)

## Context

Since ADR 015 the mode registry merged three sources at every load:
hardcoded profile `mode_specs` < `GC_MODES_FILE` TOML < in-space
Activity Mode objects. That precedence was a migration ramp that
outlived its usefulness:

- **Shadowing hazard.** The same mode name could be defined in three
  places; which definition ran depended on merge order the human
  editing the space could not see. An in-space object silently masked a
  file edit and vice versa.
- **Two ways to do one thing.** Modes were already fully expressible
  in-space (ADR 015 amendment, ADRs 029/030/033 all grew in-space
  fields); the code and TOML copies existed only as defaults.
- **Profiles as a config dumping ground.** `DomainProfile` had accreted
  mode specs, a default mode (retired by ADR 034), tool docs, role
  overrides, timeline config, and ranking weights — framing, storage
  semantics, and behavior in one hardcoded bundle.

## Decision

**The space's Activity Mode objects are the ONLY live source of mode
specs.** `load_registry(in_space, space_context)` takes the two store
payloads and nothing else; profile `mode_specs`/`default_mode` are
deleted, and the TOML no longer merges at load. The validation seam
(`spec_from_mapping`, `slugify`) moves to the layering-neutral
`interface/mode_config.py` (nothing may import the orchestrator, and
the seeder runs in infrastructure/composition).

**The modes TOML is a SEEDER.** `interface/mode_seeds/{fiction,
workspace,assistant}.toml` package the retired profile mode specs
verbatim (`[modes.<name>]` tables plus seed-only `default`/`icon`
keys); a deployment's `modes_file` (spaces.toml/channels.toml) or
`GC_MODES_FILE` overrides the packaged set — same key names, repurposed
semantics, announced by a startup log line. Composition parses the
corpus loudly on every startup and hands payloads (ModeStore-port
shaped, synthetic `seed:<slug>` ids) to:

- the **Anytype seeder** (`infrastructure/anytype/mode_seeder.py`): iff
  the type-scoped search finds ZERO Activity Mode objects, mint one
  object per seed (body=goal, `gc_mode_*`/`gc_capture_*` properties,
  icon) plus the Example Mode explainer (moved out of `ensure_schema` —
  it is part of the starter kit, and the mint-time seed would otherwise
  block the heal), verify searchability with a bounded poll (the
  fresh-object settle window), and link the `default`-marked mode on
  the Space Context's `gc_default_mode` — only when that link is empty.
  A space with ANY mode object is never touched; failures raise
  (startup provisioning fails loudly, like `ensure_schema`).
- the **memory backend**: `InMemoryModeStore` pre-filled with the same
  payloads plus a fabricated Space Context payload linking the default,
  so dev/CLI runs the exact production resolution.
- the **eval runner**: seeds + per-case `[[case.modes]]` overlays (a
  case mode shadowing a seeded slug keeps the seed's id, so the default
  link never dangles).

**One heal rule, and archived objects cannot block it.** Fresh mint and
empty-space heal are the same zero-objects trigger. The Anytype API
never returns archived objects from search, so "zero" means zero
visible; reseeding creates new objects and never touches archived ones.
An all-archived space is not a supported running state (pre-035 it
crash-looped), so converting it into a working one is the right trade —
a human who wants the bot mode-less has no such state to preserve.

**Default fallback is alphabetical.** With `profile.default_mode` gone,
a space whose Space Context has no link defaults to the alphabetically
first mode with a logged hint. Backstop only: the seeder and the memory
path always set the link.

**Profiles are deprecated (WP27).** What remains of `DomainProfile`
(tool docs, role overrides, `time_property`/`time_format`, ranking) keeps
working, marked `DEPRECATED (ADR 035 / WP27)`: tool docs collapse to one
neutral code-owned set; role-override variance is dropped; the timeline
pair is REPLACED by a redesigned general-purpose timeline feature (not
migrated — the seam to preserve is the parameter tuple through
`composition.build_runtime`); ranking weights move to deployment config
(seam: `Ranker` takes `RankingWeights` via constructor). No new profile
fields.

## Migration (pre-035 spaces)

A space that relied on profile modes has the type minted and (usually)
only the mint-time Example Mode object — which blocks the heal. Steps:

1. `spaces.toml` needs no change (`profile` already selects the
   packaged seed corpus; a per-space `modes_file` may override it).
2. In each such space, archive/delete every Activity Mode object not
   authored by a human — typically just "Example Mode". (A space with
   human-authored modes is already in-space-configured; the profile
   modes it used to overlay are exactly what the seeds replace.)
3. Restart: the heal seeds the corpus and links the default; verify
   with `/mode`.

A registry that loads ONLY `example_mode` logs the missed-migration
warning naming these steps.

## Consequences

- One source of truth for modes, zero shadowing: what you see in
  Anytype is what runs. `/mode` reload semantics unchanged (loud at
  startup, degrade-to-last-good at refresh).
- A space with no modes at load fails loudly (previously impossible —
  profiles always provided some); the heal makes this reachable only
  when seeding itself failed or every mode was archived mid-run.
- Partial-seed recovery: the next startup sees a non-empty space and
  skips the heal — finish by hand or archive the partial seeds and
  restart (documented in the seeder).
- `test_mode_config.py` pins the packaged corpora (they replaced the
  profile constants); the seeder contract suite pins heal/no-op/never-
  touch/link rules and memory≡Anytype round-trip; a live E2E covers the
  settle window the mock cannot.
