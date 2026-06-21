"""Schema rules: the closed vocabulary's structural guarantees."""

import pytest

from graph_context.domain import schema
from graph_context.domain.schema import EdgeType, NodeType
from graph_context.errors import SchemaViolation


class TestValidateEdge:
    def test_legal_edge_passes(self) -> None:
        schema.validate_edge(NodeType.CHARACTER, EdgeType.MEMBER_OF, NodeType.FACTION)

    def test_illegal_source_is_rejected(self) -> None:
        with pytest.raises(SchemaViolation, match="cannot start from a Location"):
            schema.validate_edge(NodeType.LOCATION, EdgeType.KNOWS, NodeType.CHARACTER)

    def test_illegal_target_is_rejected(self) -> None:
        with pytest.raises(SchemaViolation, match="cannot point to a Character"):
            schema.validate_edge(
                NodeType.CHARACTER, EdgeType.LOCATED_AT, NodeType.CHARACTER
            )


class TestValidateNewNode:
    def test_summary_is_required(self) -> None:
        with pytest.raises(SchemaViolation, match="summary"):
            schema.validate_new_node(NodeType.CHARACTER, "Mira", "   ", None)

    def test_event_requires_story_time(self) -> None:
        with pytest.raises(SchemaViolation, match="story_time"):
            schema.validate_new_node(NodeType.EVENT, "Siege", "A siege.", None)

    def test_non_event_does_not_require_story_time(self) -> None:
        schema.validate_new_node(NodeType.CHARACTER, "Mira", "An engineer.", None)
