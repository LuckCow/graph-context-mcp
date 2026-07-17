"""Live Space Context settings (ADR 034): one round-trip.

Creates a Space Context object through the API (as the human would in
the UI) whose ``gc_default_mode`` relation links an Activity Mode
object, and loads it back through ``AnytypeSpaceContextStore`` --
pinning that the live server stores the link as an ``objects`` relation
and that the payload matches what the mock-backed contract promises.
Two writes only: live writes are throttled to ~1 req/s.
"""

from __future__ import annotations

from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.schema_bootstrap import (
    MODE_TYPE_KEY,
    SPACE_CONTEXT_TYPE_KEY,
)
from graph_context.infrastructure.anytype.space_context_store import (
    AnytypeSpaceContextStore,
)


async def test_settings_link_round_trips_through_the_store(live_config):
    client = AnytypeClient(live_config)
    try:
        mode = await client.create_object({
            "name": "Default Candidate",
            "type_key": MODE_TYPE_KEY,
            "body": "The mode the settings object points at.",
        })
        created = await client.create_object({
            "name": "E2E Space Context",
            "type_key": SPACE_CONTEXT_TYPE_KEY,
            "properties": [
                mapping.property_entry(
                    mapping.PROP_DEFAULT_MODE, "objects", [mode["id"]]
                ),
            ],
        })
        payloads = await AnytypeSpaceContextStore(client).load()
        settings = next(
            p for p in payloads if p["name"] == "E2E Space Context"
        )
        assert settings["default_mode_ids"] == [mode["id"]]
        assert created["id"] in settings["origin"]
    finally:
        await client.aclose()
