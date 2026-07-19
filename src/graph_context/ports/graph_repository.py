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
from graph_context.domain.models import (
    Edge,
    FieldSpec,
    LinkSpec,
    Node,
    NodeDraft,
    NodeId,
    PropertyDraft,
    TimelineValue,
)
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
    * **Concurrent link mutations on one node all take effect** (ADR 009).
      However the event loop interleaves overlapping write calls, no write
      may be lost to a stale read-modify-write of a relation list. The fake
      satisfies this by being synchronously atomic; the Anytype adapter by
      serializing writes and materializing PATCH payloads from a fresh read
      inside the critical section.
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
        create_missing_fields: Mapping[str, str] | None = None,
    ) -> Node:
        """Create a node and its links.

        Resolves ``draft.type`` to an existing space type (raising
        :class:`graph_context.errors.UnknownNodeType` if none matches) and each
        link's label to an existing relation. An unknown relation label raises
        :class:`graph_context.errors.UnknownRelationLabel` unless
        ``create_missing_relations`` is set, in which case the relation is
        created. Either approval error is raised *before* any persistence.

        Story-node ``fields`` keys must resolve to existing scalar properties
        (ADR 023); an unmatched key raises
        :class:`graph_context.errors.UnknownFieldKey` before any persistence
        unless declared in ``create_missing_fields`` (key -> format from
        :data:`graph_context.domain.schema.FIELD_FORMATS`), in which case the
        property is created. Infra-role drafts are exempt: their fields are
        bookkeeping, not space vocabulary.
        """
        ...

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
        create_missing_fields: Mapping[str, str] | None = None,
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

    def relation_label_for(self, field_key: str) -> str | None:
        """The canonical edge label when ``field_key`` names an
        ``objects``-format relation, else ``None``.

        Matched exactly like a ``fields`` key resolves (by property key or
        display name, case-insensitive), because that is what this exists
        for: Anytype presents relations as properties of a type, so models
        write ``fields={"Assignee": ...}``. The tool boundary asks this
        question to route such a key as the link it really is (ADR 006:
        relations are edges) instead of surfacing a rejection.
        """
        ...

    def field_catalog(self) -> Mapping[str, tuple[FieldSpec, ...]]:
        """Reflectable scalar properties per type display name (ADR 023).

        Guidance for the LLM (overview rendering, unmatched-key errors):
        which properties already exist as ``fields`` targets on each
        non-infra type. May be empty for backends without a space schema.
        """
        ...

    async def create_type(
        self,
        name: str,
        *,
        plural: str = "",
        properties: Sequence[PropertyDraft] = (),
    ) -> str:
        """Create a NEW object type in the space (WP33, ADR 041).

        Returns the created type's display name; the type is immediately
        usable as a ``create_node`` target (implementations register it in
        their live vocabulary, no resync needed). An empty ``plural``
        derives ``<name>s``. Raises
        :class:`graph_context.errors.SchemaChangeConflict` when ``name``
        already resolves to an existing type. A property draft whose name
        matches an existing space property is REUSED (attached) when the
        formats agree, and conflicts when they differ -- formats are
        immutable (A12), so a mismatch must stop the change, never mint a
        shadow. User confirmation is the caller's contract (the schema
        tool's proposal flow); implementations do not gate.
        """
        ...

    async def add_type_properties(
        self, type_identifier: str, properties: Sequence[PropertyDraft]
    ) -> str:
        """Attach new scalar properties to an existing type (WP33).

        ``type_identifier`` resolves like ``create_node``'s type (key,
        display name, or role); no match raises
        :class:`graph_context.errors.UnknownNodeType`. Existing properties
        on the type must SURVIVE the change (the Anytype type PATCH
        replaces the property list wholesale -- quirk A11 -- so the
        adapter resends the fetched list plus the additions). Same reuse/
        conflict semantics as :meth:`create_type`; a draft already on the
        type with a matching format is a no-op, so a confirmed proposal
        can be retried safely. Returns the type's display name.
        """
        ...

    async def hydrate(self) -> None: ...

    async def resync(self) -> frozenset[NodeId]: ...

    async def fetch_body(self, node_id: NodeId) -> str:
        """On-demand retrieval of a node's long-form body.

        The body is the node's description (Prose text on Prose nodes;
        ADR 010). Bodies are intentionally absent from the GraphIndex (see
        ``NodeDraft.body``); this is the only read path for them, which
        also means a human's body edit in the Anytype UI is visible on the
        very next fetch, no resync needed. Missing/empty body returns
        ``""``; unknown id raises NodeNotFound.
        """
        ...
