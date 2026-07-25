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
    PropertyDeclaration,
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
        create_missing: Mapping[str, PropertyDeclaration] | None = None,
    ) -> Node:
        """Create a node and its links.

        Resolves ``draft.type`` to an existing space type (raising
        :class:`graph_context.errors.UnknownNodeType` if none matches) and each
        link's label to an existing relation. ``create_missing`` is ONE
        key/label -> :class:`~graph_context.domain.models.PropertyDeclaration`
        map (ADR 042) licensing new space-level vocabulary: a ``fields``
        key it declares mints a scalar property, a link label it declares
        (format ``objects``) mints a relation. Minting is space-level only
        -- the property attaches to NO type regardless of the
        declaration's ``scope`` (scope drives the *caller's* proposal
        drafting); type attachment is exclusively
        :meth:`add_type_properties`. An unmatched, undeclared ``fields``
        key raises :class:`graph_context.errors.UnknownFieldKey`; an
        unmatched, undeclared link label raises
        :class:`graph_context.errors.UnknownRelationLabel` -- either
        *before* any persistence. A declared key that already matches an
        existing same-format property is reused silently; a format
        mismatch raises :class:`graph_context.errors.SchemaChangeConflict`
        (A12: formats are immutable). Infra-role drafts are exempt from
        field resolution: their fields are bookkeeping, not space
        vocabulary.
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
        create_missing: Mapping[str, PropertyDeclaration] | None = None,
    ) -> Node:
        """Apply the non-``None`` keyword arguments and return the updated
        node. ``create_missing`` has :meth:`create_node` semantics for
        ``fields`` keys (link labels ride :meth:`add_link`'s declaration)."""
        ...

    async def add_link(
        self,
        anchor: NodeId,
        link: LinkSpec,
        *,
        create_missing: PropertyDeclaration | None = None,
    ) -> Edge:
        """Add one outgoing edge from ``anchor``. A ``create_missing``
        declaration (format ``objects``) mints the relation space-level
        when the label matches none; without one an unknown label raises
        :class:`graph_context.errors.UnknownRelationLabel`."""
        ...

    async def remove_link(self, edge: Edge) -> None: ...

    def role_for(self, type_identifier: str) -> Role | None:
        """Resolve a requested type identifier to its semantic role (or None)."""
        ...

    def known_node_types(
        self, include_roles: frozenset[Role] = frozenset()
    ) -> frozenset[str]:
        """Type names available as create_node targets (for error suggestions).

        Infra-role types are excluded unless their role is in
        ``include_roles`` -- the caller's meta privilege (ADR 045).
        """
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

    def field_catalog(
        self, include_roles: frozenset[Role] = frozenset()
    ) -> Mapping[str, tuple[FieldSpec, ...]]:
        """Reflectable scalar properties per type display name (ADR 023).

        Guidance for the LLM (overview rendering, unmatched-key errors):
        which properties already exist as ``fields`` targets on each
        non-infra type -- infra types join the catalog only when their
        role is in ``include_roles`` (ADR 045). May be empty for backends
        without a space schema.
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
        shadow. ``objects`` drafts are legal (ADR 042): a same-name
        relation attaches, joining the type's edge vocabulary; a *scalar*
        draft naming a relation conflicts (no scalar shadow of an edge,
        ADR 006). User confirmation is the caller's contract (the schema
        tool's proposal flow); implementations do not gate.
        """
        ...

    async def add_type_properties(
        self, type_identifier: str, properties: Sequence[PropertyDraft]
    ) -> str:
        """Attach new properties to an existing type (WP33; objects
        drafts legal per ADR 042, same semantics as :meth:`create_type`).

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
