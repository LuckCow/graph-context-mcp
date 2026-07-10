"""In-memory reference implementation of :class:`GraphRepository`.

Two jobs:

1. **Tests and offline development.** Application services are exercised
   against this fake, so the whole use-case layer is testable without a
   running Anytype instance.
2. **Executable specification.** ``AnytypeGraphRepository`` must match this
   behaviour exactly (see ``tests/contract``) -- in particular the
   composite-create rollback contract -- with the only difference being
   write-through persistence to the Anytype API and id assignment by
   Anytype.

Ids here are sequential (``n0001``...) purely for readable test output.
The methods are ``async`` to satisfy the port, but never actually await.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import count
from typing import Any

from graph_context.domain import schema
from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import (
    Edge,
    LinkSpec,
    Node,
    NodeDraft,
    NodeId,
    TimelineValue,
)
from graph_context.domain.schema import Role


@dataclass(frozen=True)
class FakeTemplate:
    """A type's template, as the fake models its resolved effect: default field
    values applied on create, and a scaffold body prepended to the caller's."""

    default_fields: Mapping[str, str] = field(default_factory=dict)
    body: str = ""


class InMemoryGraphRepository:
    """``GraphRepository`` whose only store is its own :class:`GraphIndex`."""

    def __init__(
        self,
        *,
        role_overrides: Mapping[str, Role] | None = None,
        templates: Mapping[str, FakeTemplate] | None = None,
    ) -> None:
        self._graph = GraphIndex()
        self._ids = count(1)
        self._bodies: dict[NodeId, str] = {}
        # Profile-supplied type-key -> Role additions (WP5); same contract
        # as the Anytype adapter's registry overrides.
        self._role_overrides: dict[str, Role] = dict(role_overrides or {})
        # Type-identifier -> template, mirroring the Anytype adapter applying a
        # type's template on create (default field values + scaffold body).
        self._templates: dict[str, FakeTemplate] = dict(templates or {})

    @property
    def graph(self) -> GraphIndex:
        return self._graph

    async def create_node(
        self,
        draft: NodeDraft,
        links: Sequence[LinkSpec] = (),
        *,
        create_missing_relations: bool = False,
    ) -> Node:
        role = schema.resolve_role(draft.type, self._role_overrides)
        # Apply the type's template (default field values + scaffold body),
        # except for infra roles -- matching the Anytype adapter. Caller fields
        # override template defaults; the caller body is appended below the
        # scaffold (template first).
        template = None if role in schema.INFRA_ROLES else self._templates.get(draft.type)
        fields = dict(draft.fields)
        body = draft.body
        if template is not None:
            fields = {**template.default_fields, **fields}
            body = (
                f"{template.body}\n{draft.body}"
                if draft.body and template.body
                else draft.body or template.body
            )
        node = Node(
            id=f"n{next(self._ids):04d}",
            # Display name mirrors the Anytype backend: a mapped role renders
            # as its role name (gc_prose -> "Prose"), else the raw identifier.
            type=role.value if role is not None else draft.type,
            name=draft.name,
            summary=draft.summary,
            story_time=draft.story_time,
            fields=fields,
            type_key=draft.type,
            role=role,
        )
        self._graph.upsert_node(node)
        if body:
            self._bodies[node.id] = body
        try:
            for link in links:
                self._graph.add_edge(link.to_edge(anchor=node.id))
        except Exception:
            # Composite-create contract: never leave a half-applied write.
            self._graph.remove_node(node.id)
            self._bodies.pop(node.id, None)
            raise
        return node

    async def update_node(
        self,
        node_id: NodeId,
        *,
        name: str | None = None,
        summary: str | None = None,
        summary_stale: bool | None = None,
        body: str | None = None,
        story_time: TimelineValue | None = None,
        fields: Mapping[str, str] | None = None,
    ) -> Node:
        changes: dict[str, Any] = {
            key: value
            for key, value in {
                "name": name,
                "summary": summary,
                "summary_stale": summary_stale,
                "story_time": story_time,
                "fields": dict(fields) if fields is not None else None,
            }.items()
            if value is not None
        }
        updated = replace(self._graph.node(node_id), **changes)
        self._graph.upsert_node(updated)
        if body is not None:
            # A7 semantics: wholesale replace; empty string clears.
            self._bodies[node_id] = body
        return updated

    async def add_link(
        self, anchor: NodeId, link: LinkSpec, *, create_missing_relations: bool = False
    ) -> Edge:
        edge = link.to_edge(anchor=anchor)
        self._graph.add_edge(edge)
        return edge

    async def remove_link(self, edge: Edge) -> None:
        self._graph.remove_edge(edge)

    def role_for(self, type_identifier: str) -> Role | None:
        return schema.resolve_role(type_identifier, self._role_overrides)

    def known_node_types(self) -> frozenset[str]:
        # The in-memory backend has an open vocabulary; surface the mapped
        # (non-infra) roles as helpful create_node suggestions.
        return frozenset(r.value for r in Role if r not in schema.INFRA_ROLES)

    def known_edge_labels(self) -> frozenset[str]:
        # No predefined relation vocabulary off a live space.
        return frozenset()

    async def hydrate(self) -> None:
        """No backing store: the index is already authoritative here."""

    async def resync(self) -> frozenset[NodeId]:
        """No out-of-band editors can exist for an in-memory store."""
        return frozenset()

    async def fetch_body(self, node_id: NodeId) -> str:
        self._graph.node(node_id)  # NodeNotFound on bad id
        return self._bodies.get(node_id, "")
