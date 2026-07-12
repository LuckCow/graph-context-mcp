# ADR 026: Space members are reflected as read-only nodes

**Status:** Accepted (2026-07-12)

## Context

Task Creation Mode's guideline "assign new tasks to the requester" was
unfulfillable end-to-end (live-caught, turns `2fb75badc8ca` /
`3543a613ff3e`). Two independent gaps:

1. The model never saw who sent a message — fixed by sender attribution
   (`[from <name>]`, `pipeline.sender_attributed`).
2. Even knowing the name, there was nothing to link an Assignee edge
   *to*. Anytype's own Assignee is an `objects`-format relation whose
   targets are **participant objects**, and those never appear in list or
   search results — the hydrate sweep cannot see them, so they were never
   in the `GraphIndex`, and edge writes pre-validate targets against the
   index.

Spike S11 (2026-07-12, live sidecar) pinned the facts: `GET
/v1/spaces/{sid}/members` enumerates members (display name, participant
id, role, status); the single-object GET serves a participant like any
object (an ordinary envelope with `type.key = "participant"`, display
name "Space member"); list and search never return them; and an
`objects`-format relation accepts a participant id as a target and
round-trips it.

## Decision

**Hydrate and resync fetch the space's active members and feed their
participant objects through the ordinary `mapping.to_node` path.**
Members become first-class, read-only nodes: visible in the overview's
type counts, findable by `find_node <name>`, and usable as link targets
(`links=[{'edge_type': 'Assignee', 'other': <member node id>}]`).

- `client.list_members()` + `sync.fetch_member_objects()` are the only
  member-specific code; from there a member is just an object envelope.
  Per-member GETs are acceptable (members are few) — this is explicitly
  not an N+1 over the space (the hydrate call-budget test allows the
  member sweep and still forbids per-object GETs).
- Resync refetches members every pass (they are invisible to the
  modified-since search); the existing stamp filter drops unchanged ones,
  so a new member is the only thing this usually adds. Member renames
  and removals reconcile on the next full hydrate — the same posture as
  the S4 archived-object blind spot.
- The fake mirrors the contract via a `members=` constructor argument;
  `MockAnytype.seed_member` models the live behavior including the
  list/search invisibility (quirk A10 in `mapping.py`).
- `SpaceRegistry.key_for_label` now also matches a relation's display
  name (like `field_property` always did for scalars), so
  `edge_type="Linked Projects"` resolves the same as `linked_projects`.

## Consequences

- "Assign this to me" works end-to-end: the sender tag names the
  requester, `find_node` finds their member node, and the Assignee
  relation links to it (certified live in
  `test_members_reflect_and_accept_assignee_links_live`).
- Members are *read-only reflections*: nothing stops a model from
  attempting `update_node` on one, but the store rejects writes to
  participants and the error surfaces normally. No special-case guard
  was added — one place (the store) owns that rule.
- A busy member (many assigned tasks) will surface among the overview's
  high-degree hubs, which is desirable ("Luckcow" becomes an entry
  point to their work).
