"""Open type vocabulary with a semantic Role layer.

v2 (space-reflecting). The system no longer maintains a closed type/edge
vocabulary: node types are whatever Anytype types exist in the user's space,
and edges are whatever ``objects``-format relations live on those objects
(bootstrapped ``gc_edge_*`` relations and human-created ones alike). What
remains here is the small set of semantic **roles** that type-aware features
key off -- timeline/``as_of`` needs to know which type means "Event", and
explore hides bookkeeping roles -- plus an *editable* type-key -> Role map and
the node-creation invariants.

This module is pure data + validation. It must never import from application,
ports, or infrastructure.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from graph_context.errors import InfraWriteDenied, SchemaViolation


class Role(StrEnum):
    """Semantic roles that drive type-aware behaviour (timeline, hiding).

    A role is *resolved* from an Anytype type key (see :func:`resolve_role`);
    an unmapped type is first-class but has no role (``None``).
    """

    EVENT = "Event"
    CHARACTER = "Character"
    LOCATION = "Location"
    ORGANIZATION = "Organization"
    TECHNOLOGY = "Technology"
    THEME = "Theme"
    ITEM = "Item"
    # Captured artifacts (fiction calls them Prose; the gc_prose type key
    # is frozen for existing spaces -- ADR 015 renamed only the concept).
    CAPTURE = "Capture"
    SESSION_CONTEXT = "SessionContext"
    INTENT = "Intent"
    # In-space activity-mode config objects (ADR 015 amendment): humans
    # browse and edit them in Anytype; the LLM's traversal never sees them.
    MODE = "ActivityMode"
    # Scheduled Event nodes (WP18, ADR 027): a fire time/cron rule plus a
    # prompt the orchestrator hands the LLM when it comes due. Humans edit
    # them in Anytype; the LLM manages them through the `schedule` tool.
    SCHEDULED = "ScheduledEvent"
    # The space-settings singleton (ADR 034): space-wide assistant config
    # humans edit in Anytype -- today, which Activity Mode new chats start
    # in. The LLM's traversal never sees it.
    SPACE_CONTEXT = "SpaceContext"
    # Automation Rule nodes (WP31, ADR 039): a watched property
    # transition plus a built-in action the rule engine runs. Humans
    # author them in Anytype; the engine writes status back.
    RULE = "AutomationRule"
    # Revision-history sidecars (WP41, ADR 049): one per tracked node,
    # body = the append-only keyframe+delta log. Bot-maintained
    # bookkeeping; humans read them in Anytype but should not edit.
    NODE_HISTORY = "NodeHistory"


# Roles that are system bookkeeping: hidden from explore by default and
# excluded from the story-node stats count.
INFRA_ROLES: frozenset[Role] = frozenset(
    {
        Role.CAPTURE, Role.SESSION_CONTEXT, Role.INTENT, Role.MODE,
        Role.SCHEDULED, Role.SPACE_CONTEXT, Role.RULE, Role.NODE_HISTORY,
    }
)


# Editable seed mapping of Anytype type *key* -> Role: common native space
# keys plus the two gc_ infrastructure types we own. A space may extend or
# override this via the repository's registry role-overrides (which is also
# where the Anytype adapter's legacy pre-pivot ``gc_`` read-compat entries
# live -- adapter knowledge, not domain).
DEFAULT_TYPE_ROLES: dict[str, Role] = {
    # native space types
    "event": Role.EVENT,
    "character": Role.CHARACTER,
    "location": Role.LOCATION,
    "organization": Role.ORGANIZATION,
    "technology": Role.TECHNOLOGY,
    "theme": Role.THEME,
    "item": Role.ITEM,
    # thin gc_ infrastructure we still own
    "gc_prose": Role.CAPTURE,
    "gc_session_context": Role.SESSION_CONTEXT,
    "gc_intent": Role.INTENT,
    "gc_activity_mode": Role.MODE,
    "gc_scheduled_event": Role.SCHEDULED,
    "gc_space_context": Role.SPACE_CONTEXT,
    "gc_rule": Role.RULE,
    "gc_node_history": Role.NODE_HISTORY,
    # The mode/scheduled types' DISPLAY names. Live spaces resolve them via
    # the gc_ keys above; backends without a key registry (the in-memory
    # repository, eval worlds) see the display name as the type, and these
    # objects must be infra-hidden there too or the two backends disagree
    # about what find_node can see.
    "activity mode": Role.MODE,
    "scheduled event": Role.SCHEDULED,
    "space context": Role.SPACE_CONTEXT,
    "automation rule": Role.RULE,
    "node history": Role.NODE_HISTORY,
}


def resolve_role(
    type_key: str, overrides: Mapping[str, Role] | None = None
) -> Role | None:
    """Resolve an Anytype type key to a semantic :class:`Role`, or ``None``.

    Matching is case-insensitive on the key. ``overrides`` (a per-space role
    map) wins over the built-in defaults. A bare role name (e.g. ``"Character"``)
    also resolves to its role, which keeps the in-memory backend and tests --
    which pass display names as the type -- working without a live registry.
    An unmapped type returns ``None`` (first-class but semantically neutral).
    """
    key = type_key.strip().lower()
    if overrides:
        for override_key, role in overrides.items():
            if override_key.strip().lower() == key:
                return role
    default = DEFAULT_TYPE_ROLES.get(key)
    if default is not None:
        return default
    for candidate in Role:
        if candidate.value.lower() == key:
            return candidate
    return None


# Scalar property formats a ``properties`` value can live in (ADR 023,
# amended by ADR 042). This is tool-surface vocabulary -- the LLM declares
# one of these when it asks for a new property via
# ``create_missing_properties`` -- so it lives in the domain, not the
# adapter (the adapter's REFLECTED_FIELD_FORMATS aliases it).
FIELD_FORMATS: frozenset[str] = frozenset(
    {
        "text",
        "number",
        "select",
        "multi_select",
        "date",
        "checkbox",
        "url",
        "email",
        "phone",
    }
)

# Everything a new-property declaration may mint (ADR 042): the scalar
# formats plus ``objects`` -- the format that makes a property a relation,
# i.e. an edge (ADR 006). Reads stay split (scalars reflect into fields,
# objects-format properties reflect as edges); creation is one vocabulary.
CREATABLE_FORMATS: frozenset[str] = FIELD_FORMATS | {"objects"}


def validate_type_name(name: str) -> None:
    """Well-formedness of a proposed NEW type's display name (WP33).

    Whether the name collides with an existing type is the repository's
    call (:class:`graph_context.errors.SchemaChangeConflict`), not ours.
    """
    if not name.strip():
        raise SchemaViolation("type 'name' must be a non-empty string")
    if name.strip().lower().startswith("gc_"):
        raise SchemaViolation(
            f"type name {name!r} uses the reserved gc_ prefix "
            "(infrastructure vocabulary); pick a human name"
        )


# Relation labels the STORE owns: they reflect as real edges on read, but
# the backing store refuses a direct write. Kept in the domain (rather than
# the Anytype quirk file) because both repository implementations answer to
# it and the interface layer may not import an adapter -- the fake mirrors
# it so `tests/contract` covers the same refusal on both backends.
#
# ``links`` is Anytype's derived outbound-reference set: an inline [[wiki]]
# link in a body lands there, so reading it is load-bearing for traversal,
# but writing it 400s with "property 'links' cannot be set directly as it
# is a reserved system property". Before this, that server error came back
# raw and un-actionable and the model retried it -- one live turn spent
# five of its sixteen tool calls in that loop. Named relations are the
# writable path.
READ_ONLY_RELATIONS: frozenset[str] = frozenset({"links"})


def is_read_only_relation(key: str) -> bool:
    """Whether ``key`` names a store-owned, read-only relation.

    Matched case-insensitively on the trimmed key, the way relation keys
    and display names resolve everywhere else.
    """
    return key.strip().casefold() in READ_ONLY_RELATIONS


def validate_infra_write(
    role: Role | None,
    type_name: str,
    admitted: frozenset[Role] = frozenset(),
    known: tuple[str, ...] = (),
) -> None:
    """The infra-write guard (ADR 045), in exactly one place.

    Generic node writes may not target infra-role objects -- mode
    config, scheduled events, rules, session state are system surfaces
    with their own owners -- unless the caller's privilege ``admitted``
    the role (today: meta-inspection admits ``Role.MODE``). The
    dedicated services (scheduler, rule engine, recorders) write through
    the repository directly and never pass here.
    """
    if role in INFRA_ROLES and role not in admitted:
        raise InfraWriteDenied(type_name, role.value, known)


def validate_new_node(
    role: Role | None,
    name: str,
    summary: str,
    story_time: float | str | None,
) -> None:
    """Enforce creation invariants from the proposal.

    * ``name`` and ``summary`` are required on every node ("forces the LLM
      to commit a one-liner at write time").
    * A node whose role is ``Event`` additionally requires ``story_time``
      (its position on the story timeline), because ``as_of`` filtering is
      meaningless without it.
    """
    if not name.strip():
        raise SchemaViolation("node 'name' must be a non-empty string")
    if not summary.strip():
        raise SchemaViolation("node 'summary' is required at creation time")
    if role is Role.EVENT and story_time is None:
        raise SchemaViolation("Event nodes require 'story_time' (timeline position)")
