"""Anytype-backed SessionStore: the ``SessionContext`` meta-node (WP3).

The snapshot JSON is stored in the ``gc_fields`` text property of a
single object of type ``gc_session_context`` -- properties are patchable
(unlike bodies, A6), and reusing an existing scalar property keeps the
bootstrap surface unchanged.

Find-or-create: the type filter on the list endpoint locates the node in
one call; if several exist (e.g. a human duplicated it in the UI), the
first is used and a warning logged -- v1 is single-user, so this is a
curiosity, not a conflict to resolve (multi-user = per-user nodes, WP4).

TODO(junior):
* Untested scaffolding -- write the contract tests described in
  WORK_PACKAGES WP3 (round-trip; corrupt JSON degrades to fresh) against
  both this and InMemorySessionStore, mirroring tests/contract.
* The SessionContext object is intentionally excluded from the graph
  workflows; note that hydrate WILL index it as a node (it is a gc_ type).
  That is harmless -- explore excludes the type by default (WP2 decision)
  -- but verify the tool layer never surfaces it accidentally.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from graph_context.domain.schema import NodeType
from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient

logger = logging.getLogger(__name__)

_SESSION_TYPE_KEY = mapping.TYPE_KEYS[NodeType.SESSION_CONTEXT]


class AnytypeSessionStore:
    def __init__(self, client: AnytypeClient) -> None:
        self._client = client
        self._object_id: str | None = None  # cached after first find/create

    async def load(self) -> dict[str, Any] | None:
        object_id = await self._find()
        if object_id is None:
            return None
        obj = await self._client.get_object(object_id)
        raw = self._fields_property(obj)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            logger.warning("SessionContext %s holds invalid JSON", object_id)
            return None

    async def save(self, snapshot: dict[str, Any]) -> None:
        object_id = await self._find()
        payload_entry = mapping.property_entry(
            mapping.PROP_FIELDS, "text", json.dumps(snapshot)
        )
        if object_id is None:
            created = await self._client.create_object({
                "name": "Session context (managed)",
                "type_key": _SESSION_TYPE_KEY,
                "properties": [
                    mapping.property_entry(mapping.PROP_SUMMARY, "text",
                                           "Server-managed session state."),
                    payload_entry,
                ],
            })
            self._object_id = created["id"]
            return
        await self._client.update_object(object_id, {"properties": [payload_entry]})

    async def _find(self) -> str | None:
        if self._object_id is not None:
            return self._object_id
        candidates = [
            obj async for obj in self._client.list_objects(type=_SESSION_TYPE_KEY)
        ]
        if not candidates:
            return None
        if len(candidates) > 1:
            logger.warning("multiple SessionContext objects; using the first")
        self._object_id = candidates[0]["id"]
        return self._object_id
