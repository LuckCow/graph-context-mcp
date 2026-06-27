"""Ports: abstract contracts the application layer depends on.

The dependency rule of this codebase: **interface -> application -> domain**,
with infrastructure plugging in *underneath* these protocols. Application
code may import this module and the domain; it must never import from
``graph_context.infrastructure``.

``GraphRepository`` deliberately exposes its :class:`GraphIndex` projection:
traversal is a first-class read path, and the index *is* the read model.
Implementations own keeping it coherent (write-through on mediated writes,
``hydrate``/``resync`` for out-of-band human edits in the Anytype UI).

All methods are ``async`` (ADR-006): the MCP SDK and the Anytype client are
async, and bridging at the tool layer proved to be a seam every feature
would trip over. The in-memory fake simply never awaits anything.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import Edge, LinkSpec, Node, NodeDraft, NodeId
from graph_context.domain.schema import Role


class GraphRepository(Protocol):
    """Persistence boundary for story-world nodes and links.

    Contract notes for implementers:

    * ``create_node`` is a *composite* write (proposal: "creating and
      linking a node is one call"). If any link fails after the node is
      created, the implementation must roll the node back -- and revert any
      partially written links -- so the store is never left half-applied.
      There are no transactions in the Anytype API, so this is
      create-then-link-then-compensate.
    * ``update_node`` applies only the keyword arguments that are not
      ``None`` and returns the updated node. The *summary staleness rule
      is not applied here* -- it is business logic owned by the
      ``NodeWriter`` use-case, which passes ``summary_stale`` explicitly.
    * Write ordering: persist to the store first, then mutate
      :attr:`graph`. The index may lag the store; it must never lead it.
    * ``hydrate`` rebuilds the index from the store (project open / full
      resync). ``resync`` applies incremental out-of-band changes and
      returns the ids of nodes that changed; callers surface these to the
      user ("N nodes changed outside this session").
    """

    @property
    def graph(self) -> GraphIndex:
        """The derived, always-current adjacency projection."""
        ...

    async def create_node(
        self,
        draft: NodeDraft,
        links: Sequence[LinkSpec] = (),
        *,
        create_missing_relations: bool = False,
    ) -> Node:
        """Create a node and its links.

        Resolves ``draft.type`` to an existing space type (raising
        :class:`graph_context.errors.UnknownNodeType` if none matches) and each
        link's label to an existing relation. An unknown relation label raises
        :class:`graph_context.errors.UnknownRelationLabel` unless
        ``create_missing_relations`` is set, in which case the relation is
        created. Either approval error is raised *before* any persistence.
        """
        ...

    async def update_node(
        self,
        node_id: NodeId,
        *,
        name: str | None = None,
        summary: str | None = None,
        summary_stale: bool | None = None,
        description: str | None = None,
        story_time: float | None = None,
        fields: Mapping[str, str] | None = None,
    ) -> Node: ...

    async def add_link(
        self, anchor: NodeId, link: LinkSpec, *, create_missing_relations: bool = False
    ) -> Edge: ...

    async def remove_link(self, edge: Edge) -> None: ...

    def role_for(self, type_identifier: str) -> Role | None:
        """Resolve a requested type identifier to its semantic role (or None)."""
        ...

    def known_node_types(self) -> frozenset[str]:
        """Type names available as create_node targets (for error suggestions)."""
        ...

    def known_edge_labels(self) -> frozenset[str]:
        """Relation labels available to reuse (for error suggestions)."""
        ...

    async def hydrate(self) -> None: ...

    async def resync(self) -> frozenset[NodeId]: ...

    async def fetch_body(self, node_id: NodeId) -> str:
        """On-demand retrieval of a node's long-form body (Prose text).

        Bodies are intentionally absent from the GraphIndex (see
        ``NodeDraft.body``); this is the only read path for them.
        Missing/empty body returns ``""``; unknown id raises NodeNotFound.
        """
        ...
