"""Idempotent schema bootstrap: ensure our Types and Properties exist.

Run once at startup (composition root) before hydrate. Safe to re-run:
existing keys are left untouched, missing ones are created. Cost: 2 list
sweeps + one create per missing item -- only the very first run against a
fresh space creates anything (8 types + 5 scalar + 10 edge properties =
23 creates, which momentarily dips into the rate-limit burst budget; that
is acceptable as a one-time setup cost, and why bootstrap runs *before*
hydrate rather than interleaved).
"""

from __future__ import annotations

import logging

from graph_context.domain.schema import NodeType
from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient

logger = logging.getLogger(__name__)


async def ensure_schema(client: AnytypeClient) -> None:
    """Create any missing gc_ types and properties in the configured space."""
    existing_types = {t["key"] async for t in client.list_types()}
    for node_type in NodeType:
        key = mapping.TYPE_KEYS[node_type]
        if key not in existing_types:
            logger.info("bootstrap: creating type %s", key)
            # plural_name is required by the API (spike). It is cosmetic,
            # human-editable display data, and bootstrap is create-if-missing,
            # so a UI rename is never clobbered -- hence a naive plural rather
            # than a maintained table (dynamic types are WP4 and carry their own).
            await client.create_type({
                "key": key,
                "name": node_type.value,
                "plural_name": f"{node_type.value}s",
                "layout": "basic",
            })

    existing_properties = {p["key"] async for p in client.list_properties()}
    for key, fmt in mapping.SCALAR_PROPERTIES.items():
        if key not in existing_properties:
            logger.info("bootstrap: creating property %s (%s)", key, fmt)
            await client.create_property({"key": key, "name": key, "format": fmt})
    for edge_type, key in mapping.EDGE_PROPERTY_KEYS.items():
        if key not in existing_properties:
            logger.info("bootstrap: creating relation property %s", key)
            await client.create_property(
                {"key": key, "name": f"edge: {edge_type.value}", "format": "objects"}
            )
