"""Derived graph overview: the cold-start entry-point map.

A fresh session has nothing held or touched, and every read path (``explore``,
``find_path``, ``get_node``) needs a node id to begin -- yet the only
id-producing tool used to report just counts. This module derives a small
"where do I start?" map straight from the :class:`GraphIndex`: per-type
counts plus the highest-degree "hub" nodes, which are the strongest entry
points and hand the caller concrete ids immediately.

It is a *derived* projection, not a maintained node: rebuilt on every call
(ms-fast at story-world scale), so there is nothing to keep in sync and
nothing polluting traversal or path results. Bookkeeping roles
(``INFRA_ROLES`` -- Prose/SessionContext) are excluded, mirroring the
story-node filter the ``context get`` stats already use.

Pure domain: imports only ``schema``, ``graph``, and ``models``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from graph_context.domain import schema
from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import Node

DEFAULT_HUB_LIMIT = 8


@dataclass(frozen=True, slots=True)
class TypeCount:
    """How many story nodes carry a given type."""

    type: str
    count: int


@dataclass(frozen=True, slots=True)
class HubNode:
    """An entry-point node and its incident-edge count."""

    node: Node
    degree: int


@dataclass(frozen=True, slots=True)
class GraphOverview:
    """Derived cold-start map. Excludes INFRA_ROLES (Prose/SessionContext)."""

    total_story_nodes: int
    type_counts: tuple[TypeCount, ...]  # desc by count, then type name
    hubs: tuple[HubNode, ...]  # desc by degree, then name, then id


def build_overview(
    graph: GraphIndex, *, hub_limit: int = DEFAULT_HUB_LIMIT
) -> GraphOverview:
    """Build the entry-point map from the current graph projection.

    ``type_counts`` lists every story type highest-first; ``hubs`` are the
    top ``hub_limit`` story nodes by degree. Both tie-break deterministically
    (type name; then node name, then id) so the rendered map is stable across
    calls -- reproducible output for tests and a steady prompt for the LLM.
    """
    story = [n for n in graph.nodes() if n.role not in schema.INFRA_ROLES]

    counts: defaultdict[str, int] = defaultdict(int)
    for node in story:
        counts[node.type] += 1
    type_counts = tuple(
        TypeCount(type=t, count=c)
        for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    )

    ranked = sorted(story, key=lambda n: (-graph.degree(n.id), n.name, n.id))
    hubs = tuple(HubNode(node=n, degree=graph.degree(n.id)) for n in ranked[:hub_limit])

    return GraphOverview(
        total_story_nodes=len(story),
        type_counts=type_counts,
        hubs=hubs,
    )
