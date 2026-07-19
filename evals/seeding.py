"""Seed a case's world through the repository port, nothing lower.

Seeding uses the same public write path as production code (``create_node``
/ ``update_node`` / ``add_link``), so a case that seeds an impossible world
fails here with the repository's own error instead of producing a world the
tools could never have built. The returned baseline counts are what
``node_count_delta`` graders measure against.

The one step outside the port is ``out_of_band`` seeds, which use the
in-memory fake's ``stage_out_of_band`` -- deliberately not a port method,
because production code must never stage phantom nodes; only the eval
fixture simulates a human editing the Anytype UI between syncs.
"""

from __future__ import annotations

from dataclasses import dataclass

from evals.dataset import EvalCase, SeedNode
from graph_context.domain.models import FieldSpec, LinkSpec, NodeDraft, NodeId
from graph_context.infrastructure.memory.fake_repository import (
    InMemoryGraphRepository,
)
from graph_context.ports.graph_repository import GraphRepository


class SeedError(Exception):
    """The case's seed world could not be built; names the failing item."""


@dataclass(frozen=True, slots=True)
class SeedResult:
    """The built world's baseline: seed handle->id plus post-seed counts."""

    ids: dict[str, NodeId]
    node_count: int
    edge_count: int


async def seed_world(repository: GraphRepository, case: EvalCase) -> SeedResult:
    ids: dict[str, NodeId] = {}
    staged: set[str] = set()  # out-of-band handles: in the space, no id yet
    if case.seed_fields or case.seed_members:
        # The space's own vocabulary: its property catalog (switches the
        # fake to the catalog-strict fields contract) and its reflected
        # members. Staged FIRST so seed nodes validate against it, exactly
        # as a live space would reject them. Fake-only, like out_of_band.
        if not isinstance(repository, InMemoryGraphRepository):
            raise SeedError(
                f"case {case.id!r}: seed.field / seed.members require "
                "the in-memory backend"
            )
        repository.stage_space_vocabulary(
            field_catalog=[
                FieldSpec(
                    name=spec.name, format=spec.format,
                    key=spec.key, options=spec.options,
                )
                for spec in case.seed_fields
            ],
            members=case.seed_members,
        )
    for spec in case.seed_nodes:
        if spec.handle in ids or spec.handle in staged:
            raise SeedError(
                f"case {case.id!r}: duplicate seed node {spec.handle!r}"
                " (same-named seeds must disambiguate with 'ref')"
            )
        if spec.out_of_band:
            # A human created this in the Anytype UI after the last sync:
            # it must stay invisible until a resync. Only the in-memory
            # fake can stage that; the eval runtime always builds one.
            if not isinstance(repository, InMemoryGraphRepository):
                raise SeedError(
                    f"case {case.id!r}: out_of_band seed {spec.handle!r} "
                    "requires the in-memory backend"
                )
            repository.stage_out_of_band(_draft(spec))
            staged.add(spec.handle)
            continue
        node = await repository.create_node(_draft(spec))
        ids[spec.handle] = node.id
        if spec.stale:
            # summary_stale is normally NodeWriter's rule; a seed sets the
            # flag directly because the STATE is the fixture, not the rule.
            await repository.update_node(node.id, summary_stale=True)
    for edge in case.seed_edges:
        for endpoint in (edge.source, edge.target):
            if endpoint in staged:
                raise SeedError(
                    f"case {case.id!r}: seed edge references {endpoint!r}, "
                    "which is out_of_band (no id until a resync mid-trial)"
                )
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
        # Out-of-band nodes exist in the space from the start, so they
        # belong to the baseline: a trial that duplicates one shows up in
        # node_count_delta as the extra node it is.
        node_count=graph.node_count() + len(staged),
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
