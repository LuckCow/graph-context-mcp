"""IntentRecorder: one provenance node per working turn (WP7 / ADR 008).

The harness records automatically what ``record_prose`` used to ask the
model to volunteer. At the end of a turn that either mutated the graph
(the journal drained non-empty) or did background work the caller traced
(ADR 038: tool calls, searches, thinking), exactly one ``gc_intent``
node is written:

* name    -- ``Intent: <first ~60 chars of the prompt> — <timestamp>``
* body    -- the verbatim user prompt (or a withheld marker, the privacy
             knob extending ``GC_STORE_LLM_INPUT``), the turn's process
             trace (ADR 038: the caller-rendered markdown replacing the
             old condensed tool list when provided), and the
             created-vs-modified detail -- capped with the truncation
             marker. Write-once *by policy* (ADR 010): a provenance
             record must not be editable, so the trace is complete AT
             CREATION -- never patched in later.
* fields  -- user / model / mode / generated-at attribution, written to
             the native attribution properties (ADR 028,
             ``domain.attribution``; in a shared space Anytype's own
             creator shows only the bot; the mode field names the activity
             mode whose binding allowed the mutation).
* links   -- one ``intent`` edge to EVERY touched node, populated at
             creation (one write per turn). A working-but-read-only turn
             links nothing; the node still records how the reply was made.

Plain-answer turns (no mutations, no trace) write nothing.

Writes go through the repository directly, NOT the journalled NodeWriter:
recording provenance must never journal itself, and intent nodes are
exempt from the summary-staleness lifecycle. Like Prose, intent nodes
never touch the session working set (infra role, hidden from traversal).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from graph_context.application.capture_recorder import TRUNCATION_MARKER, _utc_now_iso
from graph_context.application.mutation_journal import MutationRecord
from graph_context.domain import attribution
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
        process_trace: str = "",
        user_id: str = "",
        model: str = "",
        mode: str = "",
        origin: str = "",
    ) -> Node | None:
        """Persist the turn's provenance; ``None`` for a plain answer.

        ``origin`` is the transport's pointer to the triggering message
        (e.g. ``anytype:<chat_id>:<message_id>``) -- the "which
        conversation moment caused this?" half of attribution, alongside
        ``user_id``'s "who". Empty when a transport has no addressable
        messages (the CLI). ``process_trace`` is the caller-rendered
        markdown of the turn's background work (ADR 038); when present it
        becomes the body's ``gc:process`` section and, on its own, is
        reason enough to record the turn.
        """
        if not mutations and not process_trace.strip():
            return None
        stamp = self._now()
        # The privacy knob must scrub EVERY prompt surface: names render in
        # list views and summaries in Set rows, not just the body.
        shown = prompt if self._store_prompt else PROMPT_WITHHELD
        fields = {
            attribution.FIELD_USER_ID: user_id,
            attribution.FIELD_MODEL: model,
            attribution.FIELD_MODE: mode,
            attribution.FIELD_GENERATED_AT: stamp,
        }
        if origin:
            fields[attribution.FIELD_ORIGIN] = origin
        draft = NodeDraft(
            type=INTENT_TYPE,
            name=f"Intent: {_condense(shown, _NAME_PROMPT_CHARS)} — {stamp}",
            summary=_condense(shown, 200) or "(empty prompt)",
            fields=fields,
            body=_assemble_body(shown, trace, mutations, process_trace),
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
    process_trace: str = "",
) -> str:
    sections = [f"### gc:prompt\n{prompt}"]
    if process_trace.strip():
        # ADR 038: the full background process supersedes the condensed
        # call list -- one trace section, not two renderings of one turn.
        sections.append(f"### gc:process\n{process_trace.strip()}")
    elif trace:
        calls = "\n".join(f"- {t.tool}: {_condense(t.summary, 200)}" for t in trace)
        sections.append(f"### gc:tool_trace\n{calls}")
    touched = "\n".join(f"- {r.action}: {r.node_id}" for r in mutations)
    sections.append(f"### gc:touched\n{touched or '(none)'}")
    body = "\n\n".join(sections)
    if len(body) > INTENT_BODY_CAP:
        body = body[: INTENT_BODY_CAP - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return body
