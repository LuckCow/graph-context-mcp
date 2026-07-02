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

Spike S6 (resolved): bodies of 50 KB / 250 KB / 1 MB all created and
round-tripped against the live server with no practical size ceiling, and
PATCH of a body is silently ignored -- which is exactly why prose is
write-once. PROSE_BODY_CAP is therefore a *product* bound (keep stored
prompts from bloating the space without limit), not a technical one; it is
set well inside the confirmed-good range and keeps the truncation marker
because silent truncation is worse than none.

``store_llm_input=False`` (env ``GC_STORE_LLM_INPUT=0`` at the composition
root) drops the llm_input section entirely -- the WP3 privacy/size knob:
stored prompts aid debugging but bloat the space and may repeat the user's
own notes verbatim. llm_output is kept either way (it is the model's text,
usually near-identical to the prose itself).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from graph_context.domain.models import LinkSpec, Node, NodeDraft, NodeId
from graph_context.ports.graph_repository import GraphRepository

PROSE_TYPE = "gc_prose"  # thin gc_ infra type for prose passages
REFERENCES_LABEL = "references"  # Prose -> source relation label
PROSE_BODY_CAP = 500_000  # chars; product bound, well inside S6's 1 MB ceiling
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
        *,
        now: Callable[[], str] = _utc_now_iso,  # injectable for tests
        store_llm_input: bool = True,
    ) -> None:
        self._repository = repository
        self._now = now
        self._store_llm_input = store_llm_input

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
            type=PROSE_TYPE,
            name=title or _derive_title(text),
            summary=summary,
            fields={"model": model, "generated_at": self._now()},
            body=_assemble_body(
                text, llm_input if self._store_llm_input else "", llm_output
            ),
        )
        links = [
            LinkSpec(REFERENCES_LABEL, other=node_id) for node_id in references
        ]
        # Deliberately no session.touch: Prose is an infra role hidden from
        # traversal, so it must not squat on the focus stack. The sources are
        # already in focus from the reads that preceded rendering.
        return await self._repository.create_node(draft, links)


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
