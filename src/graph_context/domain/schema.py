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

from graph_context.errors import SchemaViolation


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
    PROSE = "Prose"
    SESSION_CONTEXT = "SessionContext"


# Roles that are system bookkeeping: hidden from explore by default and
# excluded from the story-node stats count.
INFRA_ROLES: frozenset[Role] = frozenset({Role.PROSE, Role.SESSION_CONTEXT})


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
    "gc_prose": Role.PROSE,
    "gc_session_context": Role.SESSION_CONTEXT,
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


def validate_new_node(
    role: Role | None,
    name: str,
    summary: str,
    story_time: float | None,
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
