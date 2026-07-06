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
from typing import Any

from graph_context.domain.schema import Role
from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.config import AnytypeApiError

logger = logging.getLogger(__name__)

# gc_ infrastructure types: (key, display name) for the role.
PROSE_TYPE_KEY = "gc_prose"
SESSION_TYPE_KEY = "gc_session_context"
INTENT_TYPE_KEY = "gc_intent"  # WP7/ADR 008: one provenance node per turn
MODE_TYPE_KEY = "gc_activity_mode"  # ADR 015 amendment: in-space mode config
INFRA_TYPES: dict[str, str] = {
    PROSE_TYPE_KEY: Role.CAPTURE.value,
    SESSION_TYPE_KEY: Role.SESSION_CONTEXT.value,
    INTENT_TYPE_KEY: Role.INTENT.value,
    MODE_TYPE_KEY: "Activity Mode",
}

# Seeded once, when the Activity Mode type is first minted: a template
# object whose body doubles as the feature's in-space documentation. The
# human edits or deletes it like any other object; deleting is permanent
# (only the TYPE is create-if-missing). It loads as a valid read-only
# mode, so switching to it makes the model explain it is a template.
EXAMPLE_MODE_NAME = "Example Mode"
EXAMPLE_MODE_SUMMARY = (
    "Template: edit this page to define an assistant behavior; "
    "changes apply when /mode is next used in chat."
)
EXAMPLE_MODE_BODY = """\
Replace this page body with the mode's goal -- the instructions the \
assistant follows while this mode is active (for example: "Record only \
what the user explicitly states; organize and link it, but never invent \
or embellish details.").

How Activity Mode objects work:

- Every object of this type is one mode the assistant can switch into.
- The object name becomes the /mode name: "Example Mode" -> /mode example_mode.
- This page body is the goal prompt handed to the model.
- Tick gc_mode_mutating to let the mode create and update nodes; unticked \
means read-only.
- Fill gc_capture_type (and optionally gc_capture_references, \
gc_capture_min_chars) to auto-capture the assistant's substantial replies \
as objects of that type.
- Edits here do NOT apply on their own: send /mode in the chat to reload \
and list modes, or /mode <name> to switch. An object named after a \
built-in mode (e.g. world_modeling) overrides it.
- Archive an object to disable its mode.
"""

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


async def ensure_schema(
    client: AnytypeClient,
    timeline: tuple[str, str] = mapping.DEFAULT_TIMELINE,
) -> None:
    """Create any missing gc_ infrastructure types and properties.

    ``timeline`` is the profile-declared Event-timeline property (ADR 015);
    a native date key an assistant profile names may not exist yet in a
    fresh space, and an inline create naming an unknown property 400s.
    """
    existing_types = {t["key"] async for t in client.list_types()}
    for key, name in INFRA_TYPES.items():
        if key not in existing_types:
            logger.info("bootstrap: creating infra type %s", key)
            payload: dict[str, Any] = {
                "key": key,
                "name": name,
                "plural_name": f"{name}s",
                "layout": "basic",
            }
            if key == MODE_TYPE_KEY:
                # Inline properties attach to the type so the fields show
                # in the UI's object editor (live-confirmed 2026-07-06);
                # they also become space properties, so the loop below
                # skips them.
                payload["properties"] = [
                    {"key": prop, "name": prop, "format": fmt}
                    for prop, fmt in mapping.MODE_PROPERTIES.items()
                ]
            await client.create_type(payload)
            if key == MODE_TYPE_KEY:
                await _seed_example_mode(client)

    existing_properties = {p["key"] async for p in client.list_properties()}
    timeline_key, timeline_format = timeline
    if timeline_key not in existing_properties:
        logger.info("bootstrap: creating timeline property %s (%s)",
                    timeline_key, timeline_format)
        await client.create_property(
            {"key": timeline_key, "name": timeline_key, "format": timeline_format}
        )
        existing_properties.add(timeline_key)
    for key, fmt in mapping.SCALAR_PROPERTIES.items():
        if key not in existing_properties:
            logger.info("bootstrap: creating property %s (%s)", key, fmt)
            await client.create_property({"key": key, "name": key, "format": fmt})
    # Mode-object fields (ADR 015 amendment). A fresh mint above creates
    # them inline with the type; this covers spaces where the type predates
    # a property (partial creation, older servers).
    for key, fmt in mapping.MODE_PROPERTIES.items():
        if key not in existing_properties:
            logger.info("bootstrap: creating property %s (%s)", key, fmt)
            await client.create_property({"key": key, "name": key, "format": fmt})
    for key, name in DEFAULT_EDGE_RELATIONS:
        if key not in existing_properties:
            logger.info("bootstrap: creating relation property %s", key)
            await client.create_property({"key": key, "name": name, "format": "objects"})


async def _seed_example_mode(client: AnytypeClient) -> None:
    """The one-time template/explainer object (see EXAMPLE_MODE_BODY).

    Best-effort: a failure here must not block startup -- the feature
    works without the template, and the README carries the same guide.
    """
    try:
        await client.create_object({
            "name": EXAMPLE_MODE_NAME,
            "type_key": MODE_TYPE_KEY,
            "body": EXAMPLE_MODE_BODY,
            "properties": [
                mapping.property_entry(
                    mapping.PROP_SUMMARY, "text", EXAMPLE_MODE_SUMMARY
                ),
            ],
        })
    except AnytypeApiError:
        logger.warning(
            "bootstrap: could not seed the example Activity Mode", exc_info=True
        )
