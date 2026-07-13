"""Anytype-backed SessionStore: keyed ``SessionContext`` meta-nodes (WP8).

Each session (chat, channel, or client) owns ONE object of type
``gc_session_context``, discriminated by the ``gc_session_key`` text
property (ADR 021); the snapshot JSON is stored in the ``gc_chat_session``
text property (ADR 028) -- properties are patchable (unlike bodies, A6).

Find-or-create: a type-scoped ``POST /search`` locates candidates (spike
S2 settled that ``GET /objects`` takes no filters), then the key match is
client-side over each candidate's ``gc_session_key`` property. Nodes
without the property are strays (e.g. a human created one by hand): they
match NO key and are reported once so the human can delete them. If
several nodes carry one key (e.g. a human duplicated one in the UI), the
first is used and a warning logged.

Note: hydrate indexes session objects as nodes (they are real objects in
the space). That is by design and harmless -- their role is in
``schema.INFRA_ROLES``, so explore hides them by default (WP2 decision) and
the story-node stats skip them; only an explicit ``include_types`` surfaces them.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.schema_bootstrap import SESSION_TYPE_KEY
from graph_context.ports.session_store import require_session_key

logger = logging.getLogger(__name__)

_SESSION_TYPE_KEY = SESSION_TYPE_KEY


class AnytypeSessionStore:
    """``labels`` (optional, shared/mutable) maps session keys to a human
    name for the node title -- e.g. the chat's display name. Filled by the
    transport before a session's first save; a missing label falls back
    to the key itself, which is still findable."""

    def __init__(
        self, client: AnytypeClient, labels: Mapping[str, str] | None = None
    ) -> None:
        self._client = client
        self._labels = labels if labels is not None else {}
        self._object_ids: dict[str, str] = {}  # key -> id, cached on find/create
        self._reported_strays: set[str] = set()

    async def load(self, key: str) -> dict[str, Any] | None:
        object_id = await self._find(require_session_key(key))
        if object_id is None:
            return None
        obj = await self._client.get_object(object_id)
        raw = self._property_text(obj, mapping.PROP_CHAT_SESSION)
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            logger.warning("SessionContext %s holds invalid JSON", object_id)
            return None
        if not isinstance(parsed, dict):
            logger.warning("SessionContext %s JSON is not an object", object_id)
            return None
        return parsed

    async def save(self, snapshot: dict[str, Any], key: str) -> None:
        key = require_session_key(key)
        object_id = await self._find(key)
        payload_entry = mapping.property_entry(
            mapping.PROP_CHAT_SESSION, "text", json.dumps(snapshot)
        )
        if object_id is None:
            label = self._labels.get(key, key)
            created = await self._client.create_object({
                "name": f"Session context — {label}",
                "type_key": _SESSION_TYPE_KEY,
                "properties": [
                    mapping.property_entry(mapping.PROP_SUMMARY, "text",
                                           "Server-managed session state."),
                    mapping.property_entry(mapping.PROP_SESSION_KEY, "text", key),
                    payload_entry,
                ],
            })
            self._object_ids[key] = created["id"]
            return
        await self._client.update_object(object_id, {"properties": [payload_entry]})

    async def _find(self, key: str) -> str | None:
        if key in self._object_ids:
            return self._object_ids[key]
        # Spike S2/S5: type-scoping lives in POST /search (GET /objects rejects
        # filters); the key match is client-side over the candidates'
        # properties (search results carry them, same read path as resync).
        matches = []
        async for obj in self._client.search(types=[_SESSION_TYPE_KEY]):
            candidate_key = self._property_text(obj, mapping.PROP_SESSION_KEY)
            if not candidate_key:
                self._report_stray(obj)
                continue
            if candidate_key == key:
                matches.append(obj)
        if not matches:
            return None
        if len(matches) > 1:
            logger.warning(
                "multiple SessionContext objects for key %r; using the first", key
            )
        self._object_ids[key] = str(matches[0]["id"])
        return self._object_ids[key]

    def _report_stray(self, obj: dict[str, Any]) -> None:
        object_id = str(obj.get("id", ""))
        if object_id in self._reported_strays:
            return
        self._reported_strays.add(object_id)
        logger.warning(
            "unkeyed session node %s (%r); ignoring -- delete it",
            object_id, obj.get("name", ""),
        )

    @staticmethod
    def _property_text(obj: dict[str, Any], prop_key: str) -> str:
        for entry in obj.get("properties", []):
            if entry.get("key") == prop_key:
                return str(entry.get("text") or "")
        return ""
