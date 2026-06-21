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

from graph_context.domain.schema import EdgeType, NodeType

NodeId = str
"""Opaque node identifier, owned by the storage layer."""


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed, typed link between two nodes."""

    source: NodeId
    type: EdgeType
    target: NodeId


@dataclass(frozen=True, slots=True)
class LinkSpec:
    """One link requested as part of a composite write.

    ``outgoing=True`` means the edge runs *from* the node being written
    *to* ``other``; ``False`` reverses it (e.g. creating an Event and
    linking an existing Character via ``participated_in`` requires an
    incoming edge: Character -> Event).
    """

    edge_type: EdgeType
    other: NodeId
    outgoing: bool = True

    def to_edge(self, anchor: NodeId) -> Edge:
        """Materialise this spec relative to the node being written."""
        if self.outgoing:
            return Edge(source=anchor, type=self.edge_type, target=self.other)
        return Edge(source=self.other, type=self.edge_type, target=anchor)


@dataclass(frozen=True, slots=True)
class NodeDraft:
    """Everything needed to create a node, minus the storage-assigned id."""

    type: NodeType
    name: str
    summary: str
    description: str = ""
    story_time: float | None = None
    fields: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Node:
    """A persisted node. Identity is ``id``; everything else is data.

    ``summary_stale`` implements the proposal's summary lifecycle: any
    update that does not carry a fresh summary flips this to ``True``
    (rule lives in the ``NodeWriter`` use-case, not here).

    ``story_time`` is only meaningful for ``NodeType.EVENT``; it is the
    node's position on the story timeline and drives ``as_of`` filtering.
    ``fields`` holds type-specific extras we have not promoted to first-class
    attributes yet.
    """

    id: NodeId
    type: NodeType
    name: str
    summary: str
    summary_stale: bool = False
    description: str = ""
    story_time: float | None = None
    fields: Mapping[str, str] = field(default_factory=dict)
