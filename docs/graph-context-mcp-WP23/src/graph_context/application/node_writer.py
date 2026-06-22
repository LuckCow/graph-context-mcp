"""Use-case: writing nodes (the ``create_node`` / ``update_node`` tools).

This is where the proposal's write-side business rules live, and *only*
here -- the repository persists what it is told, the domain validates
structure, and this service decides policy:

    * creation invariants (summary required, Events need a timeline
      position) via :func:`schema.validate_new_node`;
    * the summary-staleness rule: any update without a fresh summary
      flags ``summary_stale = True`` (relationship-only changes count --
      the one-liner may no longer reflect who the node is connected to);
    * every touched node is pushed onto the session focus stack, which is
      what makes focus-stack defaults work downstream.

Services receive their dependencies through the constructor (constructor
injection); nothing here knows whether the repository is the in-memory
fake or the Anytype adapter.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from graph_context.domain import schema
from graph_context.domain.models import Edge, LinkSpec, Node, NodeDraft, NodeId
from graph_context.domain.session import SessionState
from graph_context.ports.graph_repository import GraphRepository


class NodeWriter:
    """Composite, rule-enforcing writes against the story-world graph."""

    def __init__(self, repository: GraphRepository, session: SessionState) -> None:
        self._repository = repository
        self._session = session

    async def create_node(
        self, draft: NodeDraft, links: Sequence[LinkSpec] = ()
    ) -> Node:
        """Create a node and its initial links as one logical operation."""
        schema.validate_new_node(
            draft.type, draft.name, draft.summary, draft.story_time
        )
        node = await self._repository.create_node(draft, links)
        self._session.touch(node.id)
        for link in links:
            if self._session.focus.top != link.other:  # keep new node on top
                self._session.recent.record(link.other)
        return node

    async def update_node(
        self,
        node_id: NodeId,
        *,
        name: str | None = None,
        summary: str | None = None,
        description: str | None = None,
        story_time: float | None = None,
        fields: Mapping[str, str] | None = None,
        add_links: Sequence[LinkSpec] = (),
        remove_links: Sequence[Edge] = (),
    ) -> Node:
        """Apply field and link changes; flag staleness unless summary is fresh."""
        self._repository.graph.node(node_id)  # fail fast on bad id

        await self._repository.update_node(
            node_id,
            name=name,
            summary=summary,
            summary_stale=self._staleness_after_update(summary),
            description=description,
            story_time=story_time,
            fields=fields,
        )
        for link in add_links:
            await self._repository.add_link(node_id, link)
        for edge in remove_links:
            await self._repository.remove_link(edge)

        self._session.touch(node_id)
        return self._repository.graph.node(node_id)

    @staticmethod
    def _staleness_after_update(summary: str | None) -> bool:
        """Proposal rule: updates without a new summary mark the node stale."""
        return summary is None
