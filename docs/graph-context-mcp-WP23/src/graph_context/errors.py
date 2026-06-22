"""Exception hierarchy for graph-context-mcp.

Every error raised by this package derives from :class:`GraphContextError`,
so callers (ultimately the MCP tool layer) can catch one base class and
translate it into a structured tool-error response.

Conventions:
    * Domain rules raise :class:`SchemaViolation`.
    * Lookups raise :class:`NodeNotFound` rather than returning ``None`` --
      "node is missing" is exceptional inside a session that just referenced it.
    * Infrastructure adapters wrap transport failures in their own subclass
      (e.g. a future ``AnytypeApiError``) defined next to the adapter.
"""

from __future__ import annotations


class GraphContextError(Exception):
    """Base class for all errors raised by this package."""


class SchemaViolation(GraphContextError):
    """A write violated the fixed v1 type vocabulary or a structural rule."""


class NodeNotFound(GraphContextError):
    """A referenced node id does not exist in the graph."""

    def __init__(self, node_id: str) -> None:
        super().__init__(f"node not found: {node_id!r}")
        self.node_id = node_id


class EmptyFocusStack(GraphContextError):
    """A query relied on the focus-stack default, but the stack is empty."""

    def __init__(self) -> None:
        super().__init__(
            "no start node given and the focus stack is empty; "
            "pass an explicit start or focus a node first"
        )
