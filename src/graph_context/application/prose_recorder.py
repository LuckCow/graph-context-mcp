"""Use-case: recording rendered prose into the graph (``record_prose``).

Prose lives in the graph (proposal, "Story layer") so consistency checks
("how was this place described last time?") are queryable. A Prose node:

* ``body``     -- the rendered text (write-once *by policy*; see
                  NodeDraft.body).
* ``summary``  -- one-liner, required like every node.
* ``fields``   -- generation metadata (generated_at).
* ``references`` edges to every source node used to generate it. These
  are *explicit only* (settled in WORK_PACKAGES WP3): no auto-referencing
  of the focus stack -- provenance must be honest.

WP7 retired the ``llm_input``/``llm_output``/``model`` parameters (and
their body sections): generation provenance is the HARNESS's job now --
the orchestrator journals every mutating turn into an intent node (ADR
008), which carries the verbatim prompt, tool trace, and model
attribution. ``record_prose`` survives as the voluntary capture path for
harness-less MCP clients: text + summary + references, nothing more.

Spike S6 (resolved): bodies of 50 KB / 250 KB / 1 MB all created and
round-tripped against the live server with no practical size ceiling.
Bodies are API-editable after all (A7, ADR 010), so prose being write-once
is a *policy* choice -- provenance must not be editable -- no longer an
API limitation. PROSE_BODY_CAP is likewise a *product* bound, not a
technical one; it is set well inside the confirmed-good range and keeps
the truncation marker because silent truncation is worse than none.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from graph_context.application.mutation_journal import MutationJournal, NullJournal
from graph_context.domain.models import LinkSpec, Node, NodeDraft, NodeId
from graph_context.ports.graph_repository import GraphRepository

PROSE_TYPE = "gc_prose"  # thin gc_ infra type for prose passages
REFERENCES_LABEL = "references"  # Prose -> source relation label
PROSE_BODY_CAP = 500_000  # chars; product bound, well inside S6's 1 MB ceiling
TRUNCATION_MARKER = "\n[truncated]"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ProseRecorder:
    """Composite write specialized for Prose payloads."""

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
    ) -> Node:
        draft = NodeDraft(
            type=PROSE_TYPE,
            name=title or _derive_title(text),
            summary=summary,
            fields={"generated_at": self._now()},
            body=_capped(text),
        )
        links = [
            LinkSpec(REFERENCES_LABEL, other=node_id) for node_id in references
        ]
        # Deliberately no session.touch: Prose is an infra role hidden from
        # traversal, so it must not squat on the focus stack. The sources are
        # already in focus from the reads that preceded rendering.
        node = await self._repository.create_node(draft, links)
        self._journal.created(node.id)  # the captured artifact (WP7)
        return node


def _derive_title(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else "Untitled prose"
    return first_line[:48] + ("…" if len(first_line) > 48 else "")


def _capped(text: str) -> str:
    if len(text) > PROSE_BODY_CAP:
        return text[: PROSE_BODY_CAP - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return text
