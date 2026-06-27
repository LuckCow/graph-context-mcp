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
    """A write violated a node-creation invariant or a structural rule."""


class ApprovalRequired(GraphContextError):
    """A write needs a new space-level type or relation the user must approve.

    The space-reflecting model reuses existing types/relations; when a write
    asks for one that does not exist yet, the writer raises this instead of
    silently creating it, so new vocabulary is an explicit, user-approved act.
    """


class UnknownNodeType(ApprovalRequired):
    """``create_node`` requested a type with no match in the space."""

    def __init__(self, requested: str, known: tuple[str, ...] = ()) -> None:
        self.requested = requested
        self.known = tuple(known)
        hint = f" Known types: {', '.join(sorted(self.known))}." if self.known else ""
        super().__init__(f"no type in this space matches {requested!r}.{hint}")


class UnknownRelationLabel(ApprovalRequired):
    """A link used a relation label with no matching existing relation."""

    def __init__(self, label: str, known: tuple[str, ...] = ()) -> None:
        self.label = label
        self.known = tuple(known)
        hint = (
            f" Existing relations: {', '.join(sorted(self.known))}." if self.known else ""
        )
        super().__init__(
            f"no existing relation matches label {label!r}; pass "
            f"create_missing_relations=true to create it.{hint}"
        )


class NodeNotFound(GraphContextError):
    """A referenced node id does not exist in the graph."""

    def __init__(self, node_id: str) -> None:
        super().__init__(f"node not found: {node_id!r}")
        self.node_id = node_id


class EmptyFocusStack(GraphContextError):
    """A query relied on the focus-stack default, but the stack is empty."""

    def __init__(self) -> None:
        super().__init__(
            "no start node given and the focus stack is empty; pass an "
            "explicit start, focus a node first, or call context "
            "action='overview' to list entry-point node ids."
        )
