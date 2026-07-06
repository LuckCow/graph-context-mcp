"""Live in-space mode config (ADR 015 amendment): one round-trip.

Creates a single Activity Mode object through the API (as the human would
in the UI) and loads it back through ``AnytypeModeStore`` -- pinning that
the live server stores the goal in the body, the ``gc_mode_*`` /
``gc_capture_*`` properties on the object, and that the payload matches
what the mock-backed contract promises. One object only: live writes are
throttled to ~1 req/s.
"""

from __future__ import annotations

from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.mode_store import AnytypeModeStore
from graph_context.infrastructure.anytype.schema_bootstrap import MODE_TYPE_KEY


async def test_mode_object_round_trips_through_the_store(live_config):
    client = AnytypeClient(live_config)
    try:
        created = await client.create_object({
            "name": "Faithful Scribe",
            "type_key": MODE_TYPE_KEY,
            "body": "Record only what the user explicitly states.",
            "properties": [
                mapping.property_entry(
                    mapping.PROP_MODE_MUTATING, "checkbox", True
                ),
                mapping.property_entry(
                    mapping.PROP_CAPTURE_TYPE, "text", "gc_prose"
                ),
                mapping.property_entry(
                    mapping.PROP_CAPTURE_MIN_CHARS, "number", 120
                ),
            ],
        })
        payloads = await AnytypeModeStore(client).load()
        scribe = next(p for p in payloads if p["name"] == "Faithful Scribe")
        assert scribe["goal"] == "Record only what the user explicitly states."
        assert scribe["mutating"] is True
        assert scribe["capture"] is not None
        assert scribe["capture"]["artifact_type"] == "gc_prose"
        assert int(scribe["capture"]["min_chars"]) == 120
        assert created["id"] in scribe["origin"]
    finally:
        await client.aclose()
