"""Use-case: recording rendered prose into the graph (``record_prose``).

Prose lives in the graph (proposal, "Story layer") so consistency checks
("how was this place described last time?") are queryable. A Prose node:

* ``body``     -- the rendered text, then delimited llm_input/llm_output
                  sections (write-once; see NodeDraft.body / A5+A6).
* ``summary``  -- one-liner, required like every node.
* ``fields``   -- generation metadata (model, generated_at).
* ``references`` edges to every source node used to generate it. These
  are *explicit only* (settled in WORK_PACKAGES WP3): no auto-referencing
  of the focus stack -- provenance must be honest.

TODO(junior):
* PROSE_BODY_CAP is a placeholder pending spike S6 (max practical body
  size). Keep the truncation marker -- silent truncation is worse than
  none.
* A config flag to skip storing llm_input entirely (privacy/size; see
  WP3 open questions).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from graph_context.domain.models import LinkSpec, Node, NodeDraft, NodeId
from graph_context.domain.schema import EdgeType, NodeType
from graph_context.domain.session import SessionState
from graph_context.ports.graph_repository import GraphRepository

PROSE_BODY_CAP = 60_000  # chars; placeholder pending spike S6
SECTION_DELIM = "\n\n---\n"
LLM_INPUT_HEADER = "### gc:llm_input"
LLM_OUTPUT_HEADER = "### gc:llm_output"
TRUNCATION_MARKER = "\n[truncated]"


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class ProseRecorder:
    """Composite write specialized for Prose payloads."""

    def __init__(
        self,
        repository: GraphRepository,
        session: SessionState,
        *,
        now: Callable[[], str] = _utc_now_iso,  # injectable for tests
    ) -> None:
        self._repository = repository
        self._session = session
        self._now = now

    async def record(
        self,
        *,
        text: str,
        summary: str,
        references: Sequence[NodeId],
        title: str = "",
        llm_input: str = "",
        llm_output: str = "",
        model: str = "",
    ) -> Node:
        draft = NodeDraft(
            type=NodeType.PROSE,
            name=title or _derive_title(text),
            summary=summary,
            fields={"model": model, "generated_at": self._now()},
            body=_assemble_body(text, llm_input, llm_output),
        )
        links = [
            LinkSpec(EdgeType.REFERENCES, other=node_id) for node_id in references
        ]
        node = await self._repository.create_node(draft, links)
        self._session.touch(node.id)
        return node


def _derive_title(text: str) -> str:
    first_line = text.strip().splitlines()[0] if text.strip() else "Untitled prose"
    return first_line[:48] + ("…" if len(first_line) > 48 else "")


def _assemble_body(text: str, llm_input: str, llm_output: str) -> str:
    parts = [text]
    if llm_input:
        parts.append(f"{LLM_INPUT_HEADER}\n{llm_input}")
    if llm_output:
        parts.append(f"{LLM_OUTPUT_HEADER}\n{llm_output}")
    body = SECTION_DELIM.join(parts)
    if len(body) > PROSE_BODY_CAP:
        body = body[: PROSE_BODY_CAP - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return body
