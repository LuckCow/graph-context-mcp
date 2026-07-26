# ADR 047: Type-scoped write resolution — unattached space vocabulary needs an explicit attach

Date: 2026-07-25
Status: accepted (amends ADR 042's resolution rule — "what the space
says the key IS" becomes "what the TYPE says"; amends ADR 023's
write-match universe the same way)

## Context

A dogfooding incident (surfaced reviewing turn `45c7a5d783d8`, rooted
in turn `e71063827519`, 2026-07-20): the Todolist space's Task type
carries Anytype's built-in `linked_projects` / "Linked Projects"
relation, but the model wrote `properties={"Linked Project": ...}` —
singular. Resolution missed (matching is deliberately exact), the
unmatched-key error listed only scalar properties (relations were
invisible in it), and the model concluded the vocabulary didn't exist:
it declared `create_missing_properties={"Linked Project": {"format":
"objects", "scope": "type"}}` and minted a near-duplicate. The
`scope="type"` mint created the space-level property immediately; its
EXTEND_TYPE proposal was never 👍-approved, so the duplicate stayed
attached to NO type — a "local" property living only through
per-instance attachment.

That should have been self-limiting. It wasn't, because **bare write
resolution ran against the space-wide catalog**: every later
`create_node(type=Task, properties={"Linked Project": ...})` silently
matched the unattached duplicate and spread it object by object, while
the type's real "Linked Projects" relation sat unused. The space-wide
reuse behavior — designed to maximize matches — is what turned one
mistaken mint into a growing shadow vocabulary.

## Decision

Bare `properties` keys (scalars and relations alike) resolve against
the write's TYPE scope, not the space:

1. **create_node**: only the target type's attached properties match.
2. **update_node / add_link**: the type's attached properties PLUS
   properties the object itself already carries — an instance-attached
   (local) property stays editable on ITS object; another object's
   local property never leaks in. The instance set is index-derived
   (`Node.fields` keys; outgoing-edge property keys), accepting two
   edges: a local property whose value was cleared drops out until
   redeclared, and out-of-band local additions appear after resync.
   No store GET joins the write hot path.
3. **`create_missing_properties` widens resolution to the whole
   space**: a declared key matching an existing same-format space
   property — attached or not — is REUSED (the write attaches it to
   this object; never a twin). A format mismatch stays the A12
   `SchemaChangeConflict`. Only a key matching nothing mints. This
   makes one explicit declaration the deliberate path to put
   unattached space vocabulary on an object.
4. **Pending proposals never count.** The ADR 041 ledger holds drafts
   the repositories never consult; the real exposure was the
   `scope="type"` immediate space mint, which rule 1 now excludes from
   bare resolution. A 👍-applied `add_type_properties` registers into
   the live registry and is bare-usable at once.
5. **Exemptions** (space-wide as before): infra-role targets — the
   scheduler, recorders, and rule-engine bookkeeping write
   bootstrap-guaranteed `gc_` keys no type needs to claim — and the
   seeded `gc_edge_*` starter relations (user decision): they are
   deliberately type-less, bootstrap-owned (never an accidental
   duplicate), and the story-world profile links freely with them.
   Amended 2026-07-26 (turn a0d7b7350c34): the ADR 028 attribution
   stamps are exempt BY KEY, not just via infra-role targets — the
   capture recorder stamps `gc_generated_at` onto the mode's artifact
   type, which ADR 015 allows to be NATIVE (e.g. a Chapter), where the
   role-based exemption never applies and no model is in the loop to
   answer the teaching error; the bot-owned stamps are the scalar
   mirror of the `gc_edge_*` carve-out.

The space-wide matchers (`field_property` / `key_for_label`) survive as
the REUSE universe — declared reuse, schema-change apply, error
guidance — and stop being the bare universe. No fuzzy/near-miss
matching anywhere: a misspelling errors plainly (a considered
alternative — blocking near-duplicate names — was rejected in favor of
this structural fix; ADR 014's fuzzy-never-resolves stance stands).

**Enforcement seams.** The port's `relation_label_for` takes a
mandatory scope (`on_type` XOR `on_node`; `ValueError` otherwise), so
the tool boundary's link-vs-scalar split cannot be asked space-wide.
Each backend implements the rule once: the adapter in
`_scoped_field_property`/`_scoped_relation_key`
(`infrastructure/anytype/repository.py`, over the registry's new
`attached_property`/`attached_relation_key`), the fake in `_admits`
over per-type attachment state (`attachments` constructor/staging
surface; `_type_props` also absorbs WP33-minted types, so a schema
apply attaches there exactly like the adapter's `register_type`). The
shared contract suite certifies both; the fake's catalog WITHOUT an
`attachments` mapping keeps the historical flat behavior (every
property usable on every type) so vocabulary-only fixtures and eval
seeding stay meaningful, and open mode is untouched.

**Errors teach the attach gesture.** The unmatched-key error is
sectioned: the type's own properties (with select options) and
relations first — bare-usable — then space vocabulary NOT attached to
the type, labeled as reachable via `create_missing_properties`. When
the key exactly matches unattached space vocabulary (the incident
shape), the message says so first, with the exact declaration to
resend. `UnknownRelationLabel` gets the same sections for links.

## Consequences

* The incident becomes impossible to repeat silently: after the
  duplicate is cleaned up, `"Linked Project"` errors with "Relations on
  Task: … linked_projects …" in the message; even before cleanup, bare
  writes of the duplicate stopped resolving.
* Two old contract pins flipped by design: a declaration's mint is no
  longer bare-usable on a *different* object
  (`test_a_minted_property_is_not_bare_usable_on_another_object`,
  `test_a_minted_relation_needs_redeclaring_on_another_object`) — the
  redeclaration reuses, so no twin ever mints.
* The rule-engine invariant sharpens: rules bind per-type buckets, and
  bare writes now resolve per-type too — "type-attach is what makes a
  property rule-watchable" (ADR 042) has no bare-write loophole left.
* The `"(any type)"` field-catalog bucket changes meaning: still
  discoverable, no longer bare-usable (the docstrings say so).
* One-off remediation for the incident space:
  `scripts/cleanup_duplicate_linked_project.py` migrates the
  duplicate's links onto `linked_projects` and deletes the duplicate
  property (degrading to a report if any step fails).
