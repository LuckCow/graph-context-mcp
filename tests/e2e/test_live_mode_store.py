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
        # ADR 037: the thinking select writes by tag key -- resolve the
        # bootstrap-seeded "Xhigh" option first (reads are unthrottled).
        properties = {p["key"]: p async for p in client.list_properties()}
        thinking_prop = properties[mapping.PROP_MODE_THINKING]
        xhigh = next(
            str(t["key"])
            for t in [
                t async for t in client.list_tags(str(thinking_prop["id"]))
            ]
            if str(t.get("name")) == "Xhigh"
        )
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
                # ADR 037 driver options, all on the one throttled write.
                mapping.property_entry(
                    mapping.PROP_MODE_THINKING, "select", xhigh
                ),
                mapping.property_entry(
                    mapping.PROP_MODE_MAX_TOKENS, "number", 32000
                ),
                mapping.property_entry(
                    mapping.PROP_MODE_SEARCH_MAX_USES, "number", 3
                ),
                mapping.property_entry(
                    mapping.PROP_MODE_SEARCH_ALLOWED, "text",
                    "example.com, docs.example.com",
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
        # ADR 037: the options ride the payload the way the mock promises.
        assert scribe["thinking"] == "Xhigh"
        assert int(scribe["max_tokens"]) == 32000
        assert int(scribe["web_search_max_uses"]) == 3
        assert scribe["web_search_allowed_domains"] == (
            "example.com, docs.example.com"
        )
        assert "web_search_blocked_domains" not in scribe
    finally:
        await client.aclose()
