"""Use-case: block-anchored document edits (WP42, ADR 049).

The ``edit_document`` tool's engine: splice ONE section of a node's
body -- replace / insert_after / delete, anchored on the block-hash
vocabulary the revision history already speaks -- without the model
re-emitting the whole document. Untouched blocks are carried verbatim
by :func:`revisions.edit_body`, so their hashes (and any review marks
keyed on them) survive by construction.

Deliberately stateless: a thin composition of the repository's
``fetch_body`` and the SESSION's :class:`NodeWriter` -- routing the
final write through the writer is what makes the locked-section guard,
journal, staleness rule, and infra-write validation apply for free.
The tool layer builds one per call over ``services.writer``; plain
``update_node`` (full-body rewrite) stays valid beside this.
"""

from __future__ import annotations

from graph_context.application.node_writer import NodeWriter, WriteOutcome
from graph_context.domain import revisions, schema
from graph_context.domain.models import NodeId
from graph_context.ports.graph_repository import GraphRepository


class DocumentEditor:
    """Hash-anchored single-section edits over one node's body."""

    def __init__(
        self, repository: GraphRepository, writer: NodeWriter
    ) -> None:
        self._repository = repository
        self._writer = writer

    async def sections(
        self, node_id: NodeId
    ) -> tuple[tuple[str, str], ...]:
        """The document's current ``(hash, raw block text)`` anchors --
        works with or without a historian (hashes are derived, not
        stored)."""
        body = await self._repository.fetch_body(node_id)
        return revisions.body_blocks(body)

    async def edit(
        self,
        node_id: NodeId,
        *,
        action: str,
        anchor: str,
        text: str = "",
        summary: str | None = None,
        admitted_infra_roles: frozenset[schema.Role] = frozenset(),
    ) -> WriteOutcome:
        """Splice one section and write the result through the session's
        writer. Anchor misses raise ``SectionAnchorNotFound`` listing the
        real anchors; locked violations surface from the writer's guard."""
        body = await self._repository.fetch_body(node_id)
        new_body = revisions.edit_body(
            body, action=action, anchor=anchor, text=text
        )
        return await self._writer.update_node(
            node_id,
            description=new_body,
            summary=summary,
            admitted_infra_roles=admitted_infra_roles,
        )
