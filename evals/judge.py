"""The optional LLM judge: a rubric-scored second opinion per trial.

Code graders run first and their verdicts are never overwritten; the judge
covers what substring checks can't -- "did the reply own up to the mode
boundary", "did the answer invent relationships". Like the driver it rides
the user's Claude SUBSCRIPTION via claude-agent-sdk (never the anthropic
SDK, which would bill API credits), in a tool-less isolated session: same
``tools=[] / setting_sources=[]`` posture as ``session_options``, because
a judge that can touch the filesystem is a judge that can be prompted into
doing so.

The verdict asks for reasoning BEFORE the boolean -- reasoning-first
scoring is what makes a judge auditable (and measurably more accurate).
Judge output is model text, so parsing is tolerant: the first JSON object
found wins; an unparseable answer becomes an errored verdict, never a
crash and never a silent pass.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from evals.dataset import EvalCase
from evals.recording import TrialRecord
from graph_context.domain.graph import Direction

logger = logging.getLogger(__name__)

_SYSTEM = (
    "You are a strict grader for automated evals of a story-world "
    "knowledge-graph assistant. Judge ONLY against the rubric; do not "
    "reward style. Answer with a single JSON object, nothing else: "
    '{"reasoning": "<2-4 sentences of evidence>", "pass": true|false, '
    '"score": <0.0-1.0>}'
)


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    passed: bool
    score: float
    reasoning: str
    error: str = ""  # non-empty when the judge itself failed


async def judge_trial(
    case: EvalCase, trial: TrialRecord, model: str | None = None
) -> JudgeVerdict:
    """One rubric evaluation of one trial. Requires ``case.judge``."""
    assert case.judge is not None
    prompt = render_judge_prompt(case, trial)
    try:
        text = await _ask_claude(prompt, model)
    except Exception as err:  # noqa: BLE001 -- a judge outage must not kill the run
        logger.warning("judge failed for %s trial %d: %s",
                       case.id, trial.trial, err)
        return JudgeVerdict(False, 0.0, "", error=f"judge error: {err}")
    return parse_verdict(text)


def render_judge_prompt(case: EvalCase, trial: TrialRecord) -> str:
    """Rubric + conversation + trajectory + graph end-state, one document.

    Pure and SDK-free so tests can pin it; tool ARGUMENTS ride along (the
    rubrics reference behavior like "looked her up"), full tool results do
    not -- they are in the run's turns.jsonl when a human wants them.
    """
    assert case.judge is not None
    lines = ["<rubric>", case.judge.rubric.strip(), "</rubric>", "", "<conversation>"]
    for turn in case.turns:
        lines.append(f"user: {turn.user}")
    for kind, text in trial.replies:
        lines.append(f"assistant ({kind}): {text}")
    lines += ["</conversation>", "", "<tool_trajectory>"]
    if trial.attempted_calls:
        for call in trial.attempted_calls:
            executed = "executed" if call.name in trial.bound_tools else "rejected"
            lines.append(
                f"{call.name} ({executed}): {json.dumps(dict(call.arguments), default=str)}"
            )
    else:
        lines.append("(no tool calls)")
    lines += ["</tool_trajectory>", "", "<graph_end_state>"]
    for node in sorted(trial.graph.nodes(), key=lambda n: n.name):
        stale = " [summary stale]" if node.summary_stale else ""
        lines.append(f"{node.type} {node.name!r}: {node.summary}{stale}")
        for edge in trial.graph.edges(node.id, Direction.OUT):
            lines.append(f"  -[{edge.type}]-> {trial.graph.node(edge.target).name}")
    lines += ["</graph_end_state>"]
    return "\n".join(lines)


def parse_verdict(text: str) -> JudgeVerdict:
    """The first JSON object in the judge's answer, read leniently."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return JudgeVerdict(False, 0.0, "", error=f"no JSON in judge output: {text[:200]!r}")
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return JudgeVerdict(False, 0.0, "", error=f"unparseable judge JSON: {text[:200]!r}")
    if not isinstance(data, dict) or not isinstance(data.get("pass"), bool):
        return JudgeVerdict(False, 0.0, "", error=f"judge JSON missing 'pass': {text[:200]!r}")
    raw_score = data.get("score", 1.0 if data["pass"] else 0.0)
    score = float(raw_score) if isinstance(raw_score, int | float) else 0.0
    return JudgeVerdict(
        passed=data["pass"],
        score=min(1.0, max(0.0, score)),
        reasoning=str(data.get("reasoning", "")),
    )


async def _ask_claude(prompt: str, model: str | None) -> str:
    # Lazy import: --judge is the only path that needs the [orchestrator]
    # extra; scripted CI runs must never touch it.
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        TextBlock,
    )

    options = ClaudeAgentOptions(
        tools=[],  # [] = no Claude Code built-ins; None would mean all of them
        setting_sources=[],  # no host settings -> no injected servers/hooks
        system_prompt=_SYSTEM,
        model=model,
        max_turns=1,
    )
    parts: list[str] = []
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for message in client.receive_response():
            if isinstance(message, AssistantMessage):
                parts.extend(
                    block.text for block in message.content
                    if isinstance(block, TextBlock)
                )
    return "\n".join(parts)
