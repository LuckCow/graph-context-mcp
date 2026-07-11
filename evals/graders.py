"""Code graders: deterministic pass/fail over a trial's observable outcome.

Outcome-first by construction (the eval design's core rule): graph graders
read the end-state, tool graders bound the trajectory loosely (a forbidden
tool, a ceiling) without prescribing a sequence, reply graders are
case-insensitive substring checks. Every result carries evidence text so a
failed grade in the report reads like a finding, not a boolean.

Name resolution goes through :meth:`GraphIndex.find_by_name` -- graders
name nodes the way the model does, so a grader can assert on nodes the
MODEL created (no seeded id exists for those).
"""

from __future__ import annotations

from dataclasses import dataclass

from evals.dataset import EdgeRef, EvalCase, NodeRef
from evals.recording import TrialRecord
from graph_context.domain.graph import Direction, GraphIndex
from graph_context.domain.models import Node
from graph_context.orchestrator.turn_log import OFF_VALUES


@dataclass(frozen=True, slots=True)
class GradeResult:
    grader: str  # e.g. "graph.node_count_delta", "tools.not_executed"
    passed: bool
    detail: str  # evidence for the report


def grade(case: EvalCase, trial: TrialRecord) -> list[GradeResult]:
    if trial.harness_error:
        return [GradeResult("harness", False, trial.harness_error)]
    results: list[GradeResult] = []
    results.extend(_grade_graph(case, trial))
    results.extend(_grade_tools(case, trial))
    results.extend(_grade_session(case, trial))
    results.extend(_grade_reply(case, trial))
    return results


def _grade_graph(case: EvalCase, trial: TrialRecord) -> list[GradeResult]:
    expect = case.graph
    graph = trial.graph
    results = []
    if expect.node_count_delta is not None:
        actual = trial.node_delta
        results.append(GradeResult(
            "graph.node_count_delta",
            actual == expect.node_count_delta,
            f"expected {expect.node_count_delta:+d} nodes, got {actual:+d} "
            f"({trial.baseline_nodes} -> {graph.node_count()})",
        ))
    for ref in expect.node_exists:
        found = _find(graph, ref)
        hit = [n for n in found if _fields_ok(n, ref)]
        if hit:
            detail = f"found {hit[0].id}"
        elif found:
            detail = (
                f"{found[0].id} exists but fails field checks: "
                f"{_field_detail(found[0], ref)}"
            )
        else:
            detail = "no such node"
        results.append(GradeResult(
            "graph.node_exists", bool(hit), f"{_ref_label(ref)}: {detail}"
        ))
    for ref in expect.node_absent:
        found = _find(graph, ref)
        hit = [n for n in found if _fields_ok(n, ref)]
        if hit:
            detail = f"unexpectedly present as {hit[0].id}"
        elif found:
            detail = "absent (a same-named node exists but fails the field checks)"
        else:
            detail = "absent"
        results.append(GradeResult(
            "graph.node_absent", not hit, f"{_ref_label(ref)}: {detail}"
        ))
    for edge_ref in expect.edge_exists:
        results.append(_grade_edge(graph, edge_ref))
    if expect.no_stale_summaries:
        stale = sorted(n.name for n in graph.nodes() if n.summary_stale)
        results.append(GradeResult(
            "graph.no_stale_summaries",
            not stale,
            "no stale summaries" if not stale else f"still stale: {', '.join(stale)}",
        ))
    return results


def _grade_edge(graph: GraphIndex, ref: EdgeRef) -> GradeResult:
    arrow = "->" if ref.directed else "<->"
    name = f"edge {ref.source} -[{ref.label or '*'}]{arrow} {ref.target}"
    source = _find(graph, NodeRef(ref.source))
    target = _find(graph, NodeRef(ref.target))
    if not source or not target:
        missing = ref.source if not source else ref.target
        return GradeResult("graph.edge_exists", False, f"{name}: node {missing!r} missing")
    target_ids = {n.id for n in target}
    direction = Direction.OUT if ref.directed else Direction.BOTH
    hit = any(
        (not ref.label or edge.type == ref.label) and (
            edge.target in target_ids
            or (not ref.directed and edge.source in target_ids)
        )
        for node in source
        for edge in graph.edges(node.id, direction)
    )
    return GradeResult(
        "graph.edge_exists", hit, f"{name}: {'found' if hit else 'not found'}"
    )


def _grade_tools(case: EvalCase, trial: TrialRecord) -> list[GradeResult]:
    expect = case.tools
    executed = [call.name for call in trial.executed_calls]
    results = []
    if expect.called_any:
        hit = sorted(set(executed) & set(expect.called_any))
        results.append(GradeResult(
            "tools.called_any",
            bool(hit),
            f"expected any of {list(expect.called_any)}; executed {executed or 'nothing'}",
        ))
    for name in expect.not_executed:
        count = executed.count(name)
        results.append(GradeResult(
            "tools.not_executed",
            count == 0,
            f"{name}: " + ("not executed" if count == 0 else f"executed {count}x"),
        ))
    if expect.max_executed_calls is not None:
        results.append(GradeResult(
            "tools.max_executed_calls",
            len(executed) <= expect.max_executed_calls,
            f"{len(executed)} executed calls (ceiling {expect.max_executed_calls})",
        ))
    if expect.max_decisions is not None:
        count = len(trial.decisions)
        results.append(GradeResult(
            "tools.max_decisions",
            count <= expect.max_decisions,
            f"{count} decisions (ceiling {expect.max_decisions})",
        ))
    return results


def _grade_session(case: EvalCase, trial: TrialRecord) -> list[GradeResult]:
    expect = case.session
    results = []
    for name in expect.working_set_holds:
        found = _find(trial.graph, NodeRef(name))
        held = any(n.id in trial.session.working_set for n in found)
        results.append(GradeResult(
            "session.working_set_holds",
            held,
            f"{name!r}: "
            + ("held" if held else "not in the working set"
               if found else "no such node"),
        ))
    scratchpad = trial.session.scratchpad.casefold()
    for needle in expect.scratchpad_contains:
        hit = needle.casefold() in scratchpad
        results.append(GradeResult(
            "session.scratchpad_contains",
            hit,
            f"{needle!r} {'found' if hit else 'missing'} in the scratchpad",
        ))
    return results


def _grade_reply(case: EvalCase, trial: TrialRecord) -> list[GradeResult]:
    expect = case.reply
    reply = trial.final_reply.casefold()
    results = []
    for needle in expect.must_mention:
        hit = needle.casefold() in reply
        results.append(GradeResult(
            "reply.must_mention",
            hit,
            f"{needle!r} {'mentioned' if hit else 'missing'} in the final reply",
        ))
    for needle in expect.must_not_mention:
        hit = needle.casefold() in reply
        results.append(GradeResult(
            "reply.must_not_mention",
            not hit,
            f"{needle!r} {'unexpectedly mentioned' if hit else 'absent'}",
        ))
    return results


def _find(graph: GraphIndex, ref: NodeRef) -> list[Node]:
    """Name/type matches only -- field checks are `_grade_graph`'s layer,
    so edge and working-set graders can share this with bare refs."""
    matches = graph.find_by_name(ref.name, node_type=ref.type)
    # Substring fallbacks are for resolving, not asserting: existence means
    # a node NAMED this exists, so exact (case-insensitive) matches only.
    wanted = ref.name.strip().casefold()
    return [n for n in matches if n.name.casefold() == wanted]


def _fields_ok(node: Node, ref: NodeRef) -> bool:
    return all(_truthy(node, key) for key in ref.fields_truthy) and not any(
        _truthy(node, key) for key in ref.fields_falsy
    )


def _truthy(node: Node, key: str) -> bool:
    # OFF_VALUES is the repo-wide set of knob-off spellings; an absent key
    # reads as "" and is therefore falsy.
    return str(node.fields.get(key, "")).strip().lower() not in OFF_VALUES


def _field_detail(node: Node, ref: NodeRef) -> str:
    problems = [
        f"{key}={node.fields.get(key)!r} (want truthy)"
        for key in ref.fields_truthy if not _truthy(node, key)
    ] + [
        f"{key}={node.fields.get(key)!r} (want falsy/absent)"
        for key in ref.fields_falsy if _truthy(node, key)
    ]
    return "; ".join(problems)


def _ref_label(ref: NodeRef) -> str:
    return f"{ref.type or 'node'} {ref.name!r}"
