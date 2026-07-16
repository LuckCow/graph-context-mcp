"""Anytype-backed ModeStore: ``gc_activity_mode`` objects (ADR 015).

An Activity Mode object is the human editing surface for one activity
mode: the object name is the mode's display name, the page body is the
goal prompt (read via :func:`mapping.body_of` -- A7/A8 quirks handled
there), and the ``gc_mode_*`` / ``gc_capture_*`` properties carry the
binding and capture policy. Archiving an object disables its mode.

The store translates representation only; it does not validate. A mode
object with an empty body or an unusable name still yields a payload --
the loader (``orchestrator/modes.py``) rejects it with an error naming
the object, so config problems read the same whether they come from the
TOML file or the space.
"""

from __future__ import annotations

import logging
from typing import Any

from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient
from graph_context.infrastructure.anytype.schema_bootstrap import MODE_TYPE_KEY

logger = logging.getLogger(__name__)


class AnytypeModeStore:
    def __init__(self, client: AnytypeClient) -> None:
        self._client = client

    async def load(self) -> list[dict[str, Any]]:
        # Type-scoped search (S2/S5: GET /objects takes no filters), then a
        # per-hit GET -- bodies are absent from search responses (A7), and
        # the body IS the goal. A handful of objects, so the extra round
        # trips are noise next to hydrate.
        hits = [obj async for obj in self._client.search(types=[MODE_TYPE_KEY])]
        payloads: list[dict[str, Any]] = []
        for hit in hits:
            obj = await self._client.get_object(hit["id"])
            if obj.get("archived"):  # archived between search and GET
                continue
            payloads.append(self._payload(obj))
        return payloads

    @staticmethod
    def _payload(obj: dict[str, Any]) -> dict[str, Any]:
        props: dict[str, Any] = {}
        for entry in obj.get("properties", []):
            fmt = entry.get("format", "")
            props[entry.get("key", "")] = entry.get(fmt)
        name = str(obj.get("name") or "")
        capture: dict[str, Any] | None = None
        artifact_type = str(props.get(mapping.PROP_CAPTURE_TYPE) or "").strip()
        if artifact_type:  # presence enables capture; empty means none
            capture = {"artifact_type": artifact_type}
            references = str(
                props.get(mapping.PROP_CAPTURE_REFERENCES) or ""
            ).strip()
            if references:
                capture["references_label"] = references
            min_chars = props.get(mapping.PROP_CAPTURE_MIN_CHARS)
            if min_chars is not None:
                capture["min_chars"] = min_chars
        payload = {
            "name": name,
            # Stripped: the live markdown export pads the body with
            # trailing whitespace/newline (observed 2026-07-06), and a
            # goal prompt's edges are never meaningful.
            "goal": mapping.body_of(obj).strip(),
            "mutating": bool(props.get(mapping.PROP_MODE_MUTATING)),
            "web_search": bool(props.get(mapping.PROP_MODE_WEB_SEARCH)),
            "capture": capture,
            "origin": f"{name or '(unnamed)'} ({obj.get('id', '?')})",
        }
        # A select: the value is a tag envelope, normalized to the option's
        # display name. Empty means "not set" -- the loader applies the
        # default; a set value is validated there (lowercased, so the
        # Title-Case options match) and a typo names this object (WP19).
        detail = mapping.field_value(
            "select", props.get(mapping.PROP_MODE_ACTIVITY_DETAIL)
        ).strip()
        if detail:
            payload["activity_detail"] = detail
        return payload
