# ADR 022: Apply type templates on create

Date: 2026-07-08
Status: accepted (supersedes the "Templates: skipped deliberately" decision in
`docs/WORK_PACKAGES.md`, ADR 010's WP)

## Context

Anytype types can carry **templates**: a template pre-populates default property
values (e.g. a task's `status = "To Do"`) and a page layout (which relations
display on the object) that a human gets for free when they create an object from
the "+" button. The server ignored them entirely — `create_node` sent one
`POST /objects` with everything inline — so bot/MCP-created objects were born
without those defaults and without the property display a human sees. In practice
an agent created a to-do with no `status`, then reasoned off the wrong field.

Templates were previously cut deliberately: ADR 010's WP called the
auto-population-vs-`create-with-body` interaction an **"unspiked collision
risk."** Two spikes have since settled that risk against a live server:

- `docs/spikes/templates-body-collision.md` — the create field is **`template_id`**
  (value = the template's object id; `templateId`/`template` are silently
  ignored); a template's body and a supplied `body` **concatenate** (template
  first); templates **cannot be minted via the API** (`type_key=template` →
  HTTP 500), they are UI-authored only; the API exposes **no "default template"**
  flag and a type may own several.
- `scripts/spike_template_props.py` — creating with `template_id` alone applies
  the template's **default property values**, and an explicitly-supplied property
  **overrides** that default rather than being ignored (verified against a real
  template defaulting `status = "To Do"`).

## Decision

**When a node's type has a template, pass its `template_id` on the existing
create POST.** No restructuring: body and properties stay inline on the same
`POST /objects`; Anytype applies the template server-side. This relies only on
the spiked behavior — the template fills defaults + layout, our inline properties
override those keys, and our body is appended below the template's.

- **Which template.** The API has no "default" flag, so we take the **first** one
  it returns for the type. The choice is isolated in
  `AnytypeGraphRepository._choose_template`, the sole policy knob (a per-type
  config override is a one-line change there).
- **Body concatenation is accepted, not fought.** Templates are headers/scaffolds;
  header-then-body is the desired result. A bodyless create yields just the
  template's body.
- **Infra roles skip templating.** Capture/SessionContext/Intent/Mode are
  bot-owned bookkeeping with write-once bodies (they already skip the connections
  footer); a human's UI scaffold must not leak into them.
- **Resolution is lazy and cached** (including the negative — most types have no
  template) via a new `AnytypeClient.list_templates(type_id)` and a
  `type_key -> template_id | None` map on the repository, cleared whenever the
  registry rebuilds (hydrate/resync) so a newly UI-authored template appears. The
  registry now carries the type object id (`TypeInfo`, mirroring `PropertyInfo`)
  because the templates route is keyed by id, not key.

## Consequences

- Bot/MCP-created objects inherit the same defaults + layout a human gets,
  fixing the missing-`status` class of bug at its root.
- Extra **reads** only: one cached `list_templates` GET per type (incl. the
  negative). No extra writes — a templated create is still a single POST.
  Non-templated and infra creates are unchanged.
- "First returned" may pick a non-default template for multi-template types; if
  the API ever surfaces a system "blank" template it would need filtering in
  `_choose_template`. Watch when `list_templates` behavior changes.
- Fakes are contracts: `MockAnytype` models the templates route + `seed_template`
  + create-time application, `InMemoryGraphRepository` takes a `templates` param,
  and `TemplateContract` (tests/contract) certifies both plus the live server
  (guard-skipped when GC-E2E owns no UI-authored template — the API can't seed
  one).
- Out of scope: the `done`-vs-`status` redundancy on some task types; defaulting
  `status` addresses the wrong-field symptom, collapsing the fields is separate.
