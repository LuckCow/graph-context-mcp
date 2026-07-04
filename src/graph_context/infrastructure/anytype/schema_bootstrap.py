"""Idempotent schema bootstrap: ensure our *infrastructure* exists.

The space-reflecting model does NOT create a type per node kind anymore --
story entities use the user's own native Anytype types. Bootstrap is now
infra-only and create-if-missing:

* the ``gc_`` types we still own (gc_prose capture artifacts -- display
  name "Capture" since ADR 015 -- the managed SessionContext node, and
  gc_intent provenance records);
* the scalar ``gc_`` properties we write onto native objects (stale flag,
  story-time, fields JSON). Not minted here: descriptions live in the
  object body (ADR 010), and summaries live in the **built-in**
  ``description`` property every space already has (ADR 011) -- so neither
  ``gc_description`` nor ``gc_summary`` is created anymore;
* a small starter vocabulary of ``gc_edge_*`` relations so the model has
  reusable relation labels for common links without an approval round-trip.
  Human-created relations (``boss``, ``triggered_by``, ...) are first-class
  too; these are merely convenient defaults.

Safe to re-run: existing keys are left untouched. Only the first run against
a space creates anything.
"""

from __future__ import annotations

import logging

from graph_context.domain.schema import Role
from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient

logger = logging.getLogger(__name__)

# gc_ infrastructure types: (key, display name) for the role.
PROSE_TYPE_KEY = "gc_prose"
SESSION_TYPE_KEY = "gc_session_context"
INTENT_TYPE_KEY = "gc_intent"  # WP7/ADR 008: one provenance node per turn
INFRA_TYPES: dict[str, str] = {
    PROSE_TYPE_KEY: Role.CAPTURE.value,
    SESSION_TYPE_KEY: Role.SESSION_CONTEXT.value,
    INTENT_TYPE_KEY: Role.INTENT.value,
}

# Starter relation vocabulary (key, display name). Reusable defaults; the
# space-reflecting reader also picks up any human-created relation.
DEFAULT_EDGE_RELATIONS: list[tuple[str, str]] = [
    ("gc_edge_knows", "edge: knows"),
    ("gc_edge_located_at", "edge: located_at"),
    ("gc_edge_member_of", "edge: member_of"),
    ("gc_edge_participated_in", "edge: participated_in"),
    ("gc_edge_caused", "edge: caused"),
    ("gc_edge_possesses", "edge: possesses"),
    ("gc_edge_parent_of", "edge: parent_of"),
    ("gc_edge_child_of", "edge: child_of"),
    ("gc_edge_references", "edge: references"),
    ("gc_edge_precedes", "edge: precedes"),
    ("gc_edge_intent", "edge: intent"),  # intent node -> touched node (WP7)
]


async def ensure_schema(client: AnytypeClient) -> None:
    """Create any missing gc_ infrastructure types and properties."""
    existing_types = {t["key"] async for t in client.list_types()}
    for key, name in INFRA_TYPES.items():
        if key not in existing_types:
            logger.info("bootstrap: creating infra type %s", key)
            await client.create_type({
                "key": key,
                "name": name,
                "plural_name": f"{name}s",
                "layout": "basic",
            })

    existing_properties = {p["key"] async for p in client.list_properties()}
    for key, fmt in mapping.SCALAR_PROPERTIES.items():
        if key not in existing_properties:
            logger.info("bootstrap: creating property %s (%s)", key, fmt)
            await client.create_property({"key": key, "name": key, "format": fmt})
    for key, name in DEFAULT_EDGE_RELATIONS:
        if key not in existing_properties:
            logger.info("bootstrap: creating relation property %s", key)
            await client.create_property({"key": key, "name": name, "format": "objects"})
