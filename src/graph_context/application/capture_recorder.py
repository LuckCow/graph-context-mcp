"""Use-case: capturing produced artifacts into the graph (WP7/WP12).

A capture is a node holding produced text (a prose passage, a recorded
procedure, meeting notes -- whatever the active mode's CapturePolicy
says), with:

* ``body``     -- the text itself (write-once *by policy*; see
                  NodeDraft.body).
* ``summary``  -- one-liner, required like every node.
* ``fields``   -- generation metadata (generated_at).
* references edges to every source node that shaped it. Explicit only
  (settled in WP3): no auto-referencing of the session working set.

ADR 015 made the artifact type configurable: ``gc_prose`` is the fiction
default and keeps the infra-role hiding; a native type (``procedure``,
``note``) produces a FIRST-CLASS node -- visible in traversal, searchable,
footered -- because a recorded procedure is work product, not bookkeeping.

Generation provenance (prompt, tool trace, attribution) is the intent
node's job (ADR 008), not the capture's. The orchestrator is the only
caller -- the record_prose tool was removed 2026-07-04.

Spike S6 (resolved): bodies of 50 KB / 250 KB / 1 MB all round-tripped
live; BODY_CAP is a *product* bound with an explicit truncation marker,
not a technical ceiling.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from graph_context.application.mutation_journal import MutationJournal, NullJournal
from graph_context.domain.models import LinkSpec, Node, NodeDraft, NodeId
from graph_context.ports.graph_repository import GraphRepository

PROSE_TYPE = "gc_prose"  # the fiction default artifact type (infra-hidden)
REFERENCES_LABEL = "references"  # default capture -> source relation label
BODY_CAP = 500_000  # chars; product bound, well inside S6's 1 MB ceiling
TRUNCATION_MARKER = "\n[truncated]"

# Compat aliases (intent_recorder imports the marker; old name kept greppable).
PROSE_BODY_CAP = BODY_CAP


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class CaptureRecorder:
    """Composite write specialized for captured artifacts."""

    def __init__(
        self,
        repository: GraphRepository,
        *,
        now: Callable[[], str] = _utc_now_iso,  # injectable for tests
        journal: MutationJournal | None = None,
    ) -> None:
        self._repository = repository
        self._now = now
        self._journal = journal or NullJournal()

    async def record(
        self,
        *,
        text: str,
        summary: str,
        references: Sequence[NodeId],
        title: str = "",
        artifact_type: str = PROSE_TYPE,
        references_label: str = REFERENCES_LABEL,
    ) -> Node:
        draft = NodeDraft(
            type=artifact_type,
            name=title or _derive_title(text),
            summary=summary,
            fields={"generated_at": self._now()},
            body=_capped(text),
        )
        links = [
            LinkSpec(references_label, other=node_id) for node_id in references
        ]
        # Deliberately no session.touch: captures record what a turn produced;
        # the sources are already in recent history from the preceding reads.
        # (gc_prose is additionally infra-hidden; native artifact types are
        # first-class and appear in traversal like any node.)
        node = await self._repository.create_node(
            draft, links, create_missing_relations=True
        )
        self._journal.created(node.id)  # the captured artifact (WP7)
        return node


def _derive_title(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else "Untitled capture"
    return first_line[:48] + ("…" if len(first_line) > 48 else "")


def _capped(text: str) -> str:
    if len(text) > BODY_CAP:
        return text[: BODY_CAP - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return text
