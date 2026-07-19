"""Anytype-backed SpaceContextStore: the ``gc_space_context`` object (ADR 034).

The Space Context object is the human editing surface for space-wide
assistant settings: its ``gc_default_mode`` relation links the Activity
Mode object NEW chats start in. Bootstrap seeds one per space; humans
edit (or recreate) it in the app.

The store translates representation only; it does not validate. Extra
Space Context objects, dangling links, and multi-target links all still
yield payloads -- the loader (``orchestrator/modes.py``) rejects them
with an error naming the object, so config problems read the same
whether they come from a TOML file or the space.
"""

from __future__ import annotations

import logging
from typing import Any

from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.schema_bootstrap import (
    SPACE_CONTEXT_TYPE_KEY,
)

logger = logging.getLogger(__name__)


class AnytypeSpaceContextStore:
    def __init__(self, client: AnytypeClient) -> None:
        self._client = client

    async def load(self) -> list[dict[str, Any]]:
        # Type-scoped search (S2/S5: GET /objects takes no filters), then a
        # per-hit GET for the relation targets and the archived flag --
        # the same shape as the mode store; normally exactly one object.
        hits = [
            obj async for obj in
            self._client.search(types=[SPACE_CONTEXT_TYPE_KEY])
        ]
        payloads: list[dict[str, Any]] = []
        for hit in hits:
            obj = await self._client.get_object(hit["id"])
            if obj.get("archived"):  # archived between search and GET
                continue
            name = str(obj.get("name") or "")
            payloads.append({
                "name": name,
                "default_mode_ids": mapping.relation_targets(
                    obj, mapping.PROP_DEFAULT_MODE
                ),
                "origin": f"{name or '(unnamed)'} ({obj.get('id', '?')})",
            })
        return payloads
