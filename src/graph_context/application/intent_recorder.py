"""IntentRecorder: one provenance node per mutating turn (WP7 / ADR 008).

The harness records automatically what ``record_prose`` used to ask the
model to volunteer. At the end of a turn whose journal drained non-empty,
exactly one ``gc_intent`` node is written:

* name    -- ``Intent: <first ~60 chars of the prompt> — <timestamp>``
* body    -- the verbatim user prompt (or a withheld marker, the privacy
             knob extending ``GC_STORE_LLM_INPUT``), a condensed tool-call
             trace, and the created-vs-modified detail -- capped with the
             truncation marker. Write-once *by policy* (ADR 010): a
             provenance record must not be editable.
* fields  -- ``user_id`` / ``model`` / ``generated_at`` attribution (in a
             shared space Anytype's own creator shows only the bot).
* links   -- one ``intent`` edge to EVERY touched node, populated at
             creation (one write per turn).

Read-only turns write nothing -- the caller simply has nothing to drain.

Writes go through the repository directly, NOT the journalled NodeWriter:
recording provenance must never journal itself, and intent nodes are
exempt from the summary-staleness lifecycle. Like Prose, intent nodes
never touch the focus stack (infra role, hidden from traversal).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from graph_context.application.capture_recorder import TRUNCATION_MARKER, _utc_now_iso
from graph_context.application.mutation_journal import MutationRecord
from graph_context.domain.models import LinkSpec, Node, NodeDraft
from graph_context.ports.graph_repository import GraphRepository

INTENT_TYPE = "gc_intent"
INTENT_EDGE_LABEL = "intent"  # single label; created-vs-modified is body detail
INTENT_BODY_CAP = 100_000  # chars; prompts + traces, not prose -- keep it lean
PROMPT_WITHHELD = "[prompt withheld by user preference]"
_NAME_PROMPT_CHARS = 60


@dataclass(frozen=True, slots=True)
class ToolTrace:
    """One condensed tool call for the body's trace section."""

    tool: str
    summary: str  # e.g. rendered arguments, pre-condensed by the caller


class IntentRecorder:
    """Journal drainings in, at most one intent node out."""

    def __init__(
        self,
        repository: GraphRepository,
        *,
        store_prompt: bool = True,
        now: Callable[[], str] = _utc_now_iso,  # injectable for tests
    ) -> None:
        self._repository = repository
        self._store_prompt = store_prompt
        self._now = now

    async def record_turn(
        self,
        *,
        prompt: str,
        mutations: Sequence[MutationRecord],
        trace: Sequence[ToolTrace] = (),
        user_id: str = "",
        model: str = "",
    ) -> Node | None:
        """Persist the turn's provenance; ``None`` for a read-only turn."""
        if not mutations:
            return None
        stamp = self._now()
        # The privacy knob must scrub EVERY prompt surface: names render in
        # list views and summaries in Set rows, not just the body.
        shown = prompt if self._store_prompt else PROMPT_WITHHELD
        draft = NodeDraft(
            type=INTENT_TYPE,
            name=f"Intent: {_condense(shown, _NAME_PROMPT_CHARS)} — {stamp}",
            summary=_condense(shown, 200) or "(empty prompt)",
            fields={
                "user_id": user_id,
                "model": model,
                "generated_at": stamp,
            },
            body=_assemble_body(shown, trace, mutations),
            icon="🧾",
        )
        links = [
            LinkSpec(INTENT_EDGE_LABEL, other=record.node_id)
            for record in mutations
        ]
        return await self._repository.create_node(
            draft, links, create_missing_relations=True
        )


def _condense(text: str, limit: int) -> str:
    flattened = " ".join(text.split())
    return flattened if len(flattened) <= limit else flattened[: limit - 1] + "…"


def _assemble_body(
    prompt: str,
    trace: Sequence[ToolTrace],
    mutations: Sequence[MutationRecord],
) -> str:
    sections = [f"### gc:prompt\n{prompt}"]
    if trace:
        calls = "\n".join(f"- {t.tool}: {_condense(t.summary, 200)}" for t in trace)
        sections.append(f"### gc:tool_trace\n{calls}")
    touched = "\n".join(f"- {r.action}: {r.node_id}" for r in mutations)
    sections.append(f"### gc:touched\n{touched}")
    body = "\n\n".join(sections)
    if len(body) > INTENT_BODY_CAP:
        body = body[: INTENT_BODY_CAP - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return body
