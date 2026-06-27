"""Core domain entities and value objects.

All models here are immutable (frozen dataclasses). Updates are expressed
as ``dataclasses.replace`` producing new values; the only mutable state in
the domain lives inside :class:`graph_context.domain.graph.GraphIndex` and
the session objects, where mutability *is* the point.

``NodeDraft`` vs ``Node``: ids are assigned by the storage layer (Anytype
mints object ids), so use-cases build a draft and receive a ``Node`` back
from the repository. This keeps id-generation policy out of the domain.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from graph_context.domain.schema import Role

NodeId = str
"""Opaque node identifier, owned by the storage layer."""


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed, labelled link between two nodes.

    ``type`` is the *cleaned display label* (e.g. ``"knows"``, ``"boss"``)
    used for filtering and rendering. ``property_key`` is the raw Anytype
    relation key the edge was read from / must be written back to (e.g.
    ``"gc_edge_knows"``, ``"triggered_by"``); ``""`` for synthetic edges or
    the in-memory backend. Both participate in identity so two genuinely
    different relations that clean to the same label stay distinct.
    """

    source: NodeId
    type: str
    target: NodeId
    property_key: str = ""


@dataclass(frozen=True, slots=True)
class LinkSpec:
    """One link requested as part of a composite write.

    ``edge_type`` is the requested relation *label*; the repository resolves
    it to an existing relation's property key (or surfaces it for approval).
    ``outgoing=True`` means the edge runs *from* the node being written *to*
    ``other``; ``False`` reverses it (e.g. creating an Event and linking an
    existing Character via ``participated_in`` requires an incoming edge:
    Character -> Event).
    """

    edge_type: str
    other: NodeId
    outgoing: bool = True

    def to_edge(self, anchor: NodeId, property_key: str = "") -> Edge:
        """Materialise this spec relative to the node being written."""
        if self.outgoing:
            return Edge(
                source=anchor, type=self.edge_type, target=self.other,
                property_key=property_key,
            )
        return Edge(
            source=self.other, type=self.edge_type, target=anchor,
            property_key=property_key,
        )


@dataclass(frozen=True, slots=True)
class NodeDraft:
    """Everything needed to create a node, minus the storage-assigned id.

    ``body`` is long-form, write-once content (Markdown). It is persisted
    to the store at creation but is deliberately **not** part of
    :class:`Node` and never enters the GraphIndex: bodies (Prose text can
    be thousands of words) would bloat hydration and the context window.
    Retrieval is on-demand via ``GraphRepository.fetch_body``. Write-once
    is also the safe posture given Anytype's documented PATCH-body
    limitation (mapping assumption A6; spike S6 confirmed PATCH of body is
    silently ignored).
    """

    type: str
    name: str
    summary: str
    description: str = ""
    story_time: float | None = None
    fields: Mapping[str, str] = field(default_factory=dict)
    body: str = ""


@dataclass(frozen=True, slots=True)
class Node:
    """A persisted node. Identity is ``id``; everything else is data.

    ``summary_stale`` implements the proposal's summary lifecycle: any
    update that does not carry a fresh summary flips this to ``True``
    (rule lives in the ``NodeWriter`` use-case, not here).

    ``story_time`` is only meaningful for nodes whose ``role`` is
    ``Role.EVENT``; it is the node's position on the story timeline and drives
    ``as_of`` filtering. ``fields`` holds type-specific extras we have not
    promoted to first-class attributes yet.

    ``type`` is the Anytype type's *display name* (rendered to the user);
    ``type_key`` is its raw key (used for writes); ``role`` is the resolved
    semantic role (``None`` for types with no mapped role).
    """

    id: NodeId
    type: str
    name: str
    summary: str
    summary_stale: bool = False
    description: str = ""
    story_time: float | None = None
    fields: Mapping[str, str] = field(default_factory=dict)
    type_key: str = ""
    role: Role | None = None
