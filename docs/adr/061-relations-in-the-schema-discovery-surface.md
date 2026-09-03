# ADR 061: A type's `objects` relations are part of its taught property vocabulary

Date: 2026-09-03
Status: accepted (completes ADR 042's unification on the READ side; amends
ADR 023's "the overview renders the catalog" to cover relations; ADR 006's
key-derived edge labels and edge-read semantics are unchanged)

## Context

A dogfooding incident in the RuneScape space (`space_setup` mode, chat
`…3tkm2q`, 2026-09-03): the model created `Skill/Goal/Requirement/Training
Method` with their `objects` relations in the initial `propose_type` calls
(p1–p4, all applied), then re-proposed the SAME relations as `propose_fields`
twice more (p5–p7, p8–p10), each time reasoning that "the earlier proposals
hadn't actually applied." They had. `add_type_properties` no-op'd cleanly, and
the transport still posted `✅ … now carries …`, so nothing contradicted the
model. And **no read surface shows a type's attached relations**:
`context action='overview'` lists only scalar properties
(`_render_field_catalog`), `get_node` on a link-less instance shows `edges:
none`, and the `schema` tool has no "show" action. Every surface the model
could see was consistent with "the relation is missing."

This is the failure mode of ADR 023 (`due` vs `Due date`), ADR 042
(`modified_at` guess), and ADR 047 (`Linked Project` near-namesake): **the
model acts wrongly on a property/relation it was never shown.** Each ADR
widened what is taught. Relations in the overview were the last gap.

### What the split actually bought

Only one thing is load-bearing: `field_catalog()` feeds
`application/rule_engine.py`, which resolves a rule's watch/action property
against the target type's real vocabulary. Rule conditions are value
*transitions* (`changed to true`, `changed`, …) — meaningful for a
checkbox/select/date, not a relation (a link has no scalar value to
transition; `modified_at` already covers "something changed"). That consumer
wants scalars only.

Everything else (`get_node` rendering links under `edges:`, `explore`/`query`
`edge_types`) is about an *instance's set links*, which genuinely are edges. A
*type's available relation vocabulary* is schema, and since ADR 042 a relation
label is usable as a `properties` key exactly like a scalar — hiding it from
the schema overview served nothing.

## Decision

**The overview's "properties by type" block lists a type's writable `objects`
relations beside its scalars**, each tagged `(objects)` so the value shape (a
node id/name) stays clear. One vocabulary, read one way.

* **New port method `relation_catalog(include_roles=frozenset())`**, the
  sibling of `field_catalog`: per-type-display-name attached `objects`
  relations, plus an `"(any type)"` bucket for space relations no type carries
  (attachable via a `create_missing_properties` declaration, ADR 047).
  Excludes `SYSTEM_RELATION_DENYLIST` and `domain.schema.READ_ONLY_RELATIONS`
  (`links`) — only relations a write can go through, matching
  `relation_label_for`. Kept SEPARATE from `field_catalog`, which stays
  scalar-only as the rule engine's input — an internal filter, not a
  user-facing distinction.
* **`registry.attached_relations` now also excludes read-only relations**
  (`links`), so the unmatched-key error stops teaching a label a write can
  never use; `registry.unattached_relations()` is the new `"(any type)"`
  source (objects props on no type, minus denylist / read-only / `gc_`).
* **The presenter merges the two catalogs** into one block per type
  (`render_overview` gains a `relation_catalog` parameter; scalars first, then
  relations). `context_tool`'s overview branch passes both, scoped by
  `visible_infra_roles` identically.

**Companion — honest no-op feedback.** `add_type_properties` returns
`(type_name, names_actually_attached)` (empty tuple when every proposed
property was already on the type); `SchemaProposals.apply` returns that as a
third element; the Anytype transport posts
`✅ p5: 'Goal' already carries Parent Goal — no change.` instead of a phantom
`now carries`. `create_type` is unchanged (a name conflict raises; every
property on a new type is genuinely new).

## Deliberately not done

Merging the two catalog methods; putting relations into `Node.fields`;
changing how set links render in `get_node`/`explore`/`query`. High risk, no
gain past the `(objects)` tag.

## Consequences

* The re-proposal loop closes from both sides: the model can see the relation
  in the overview, and a redundant confirmed proposal now says "no change."
* `relation_catalog` is contract-tested on both backends alongside
  `field_catalog` (`FieldCatalogContract`); the fake mirrors its three modes
  (open / flat / scoped).
* `field_catalog`'s scalar-only guarantee — and the rule engine that leans on
  it — is untouched. `test_relations_stay_out_of_the_fields_catalog` still
  holds.
