"""Authoring auto-capture: entity-link produced text against the graph (WP7).

The harness captures what ``record_prose`` used to hope the model would
volunteer: when an authoring-mode turn produces substantial text that
mentions known story nodes, the text becomes a Prose node with
``references`` edges to every mention -- and the turn's intent node links
to the artifact via the journal (prompt -> intent -> artifact + sources).

v1 matching is exact node names, case-insensitive, on word boundaries
(no alias table yet -- WP7 leaves semantic linking to the WP4 entry
criterion). Infra-role nodes never match; a name that is empty or pure
punctuation cannot match by construction.
"""

from __future__ import annotations

import re

from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import NodeId
from graph_context.domain.schema import INFRA_ROLES

# Below this many characters a reply is conversation, not an artifact worth
# a node. Tunable by dogfooding (WP7 open question territory).
MIN_CAPTURE_CHARS = 200


def entity_links(text: str, graph: GraphIndex) -> list[NodeId]:
    """Node ids whose names appear in ``text``, ordered by first mention."""
    lowered = text.lower()
    hits: list[tuple[int, NodeId]] = []
    for node in graph.nodes():
        if node.role in INFRA_ROLES:
            continue
        name = node.name.strip().lower()
        if not name:
            continue
        match = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", lowered)
        if match:
            hits.append((match.start(), node.id))
    hits.sort()
    return [node_id for _, node_id in hits]


def should_capture(text: str, references: list[NodeId]) -> bool:
    """An artifact is text long enough to matter that touches the world."""
    return len(text.strip()) >= MIN_CAPTURE_CHARS and bool(references)
