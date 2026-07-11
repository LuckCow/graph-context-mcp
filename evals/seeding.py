"""Seed a case's world through the repository port, nothing lower.

Seeding uses the same public write path as production code (``create_node``
/ ``update_node`` / ``add_link``), so a case that seeds an impossible world
fails here with the repository's own error instead of producing a world the
tools could never have built. The returned baseline counts are what
``node_count_delta`` graders measure against.
"""

from __future__ import annotations

from dataclasses import dataclass

from evals.dataset import EvalCase, SeedNode
from graph_context.domain.models import LinkSpec, NodeDraft, NodeId
from graph_context.ports.graph_repository import GraphRepository


class SeedError(Exception):
    """The case's seed world could not be built; names the failing item."""


@dataclass(frozen=True, slots=True)
class SeedResult:
    """The built world's baseline: name->id plus post-seed counts."""

    ids: dict[str, NodeId]
    node_count: int
    edge_count: int


async def seed_world(repository: GraphRepository, case: EvalCase) -> SeedResult:
    ids: dict[str, NodeId] = {}
    for spec in case.seed_nodes:
        if spec.name in ids:
            raise SeedError(f"case {case.id!r}: duplicate seed node {spec.name!r}")
        node = await repository.create_node(_draft(spec))
        ids[spec.name] = node.id
        if spec.stale:
            # summary_stale is normally NodeWriter's rule; a seed sets the
            # flag directly because the STATE is the fixture, not the rule.
            await repository.update_node(node.id, summary_stale=True)
    for edge in case.seed_edges:
        for endpoint in (edge.source, edge.target):
            if endpoint not in ids:
                raise SeedError(
                    f"case {case.id!r}: seed edge references {endpoint!r}, "
                    "which is not a seeded node"
                )
        await repository.add_link(
            ids[edge.source],
            LinkSpec(edge_type=edge.label, other=ids[edge.target]),
        )
    graph = repository.graph
    return SeedResult(
        ids=ids,
        node_count=graph.node_count(),
        edge_count=graph.edge_count(),
    )


def _draft(spec: SeedNode) -> NodeDraft:
    return NodeDraft(
        type=spec.type,
        name=spec.name,
        summary=spec.summary,
        story_time=spec.story_time,
        fields=dict(spec.fields),
        body=spec.body,
        icon=spec.icon,
    )
