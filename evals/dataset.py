"""Eval case files: TOML in, frozen dataclasses out, problems raised loudly.

The format follows the repo's config posture (``modes.py``,
``ranking_eval.toml``): ``[[case]]`` tables in a file with one ``[suite]``
header, every key validated against an allowlist, and every error naming
the file and case so the author knows exactly what to fix. Cases are
prompts plus graders; a silently ignored typo in either would grade the
wrong thing, which is worse than no eval at all.

Graders are outcome-first by design: ``expect.graph`` asserts on what the
world looks like afterwards, ``expect.tools`` puts loose bounds on the
trajectory (a forbidden tool, a call budget) without ever prescribing a
call sequence. ``[[case.script]]`` is the deterministic stand-in decision
list -- consumed only under ``--driver scripted`` -- and doubles as the
case's reference solution: if the script can't satisfy the graders, the
case is unsolvable and the smoke run says so.
"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graph_context.orchestrator.drivers import LLMTurn, ToolCall


class DatasetError(Exception):
    """A case file is malformed; the message names file, case, and field."""


@dataclass(frozen=True, slots=True)
class NodeRef:
    """A node named by a grader; ``type`` optionally narrows the match.

    ``fields_truthy`` / ``fields_falsy`` further constrain the match on the
    node's field values, using the repo-wide knob-off spellings ("", "0",
    "false", "no", "off") as the falsy set: truthy means the key is present
    with any other value; falsy means absent or a knob-off spelling. On
    ``node_exists`` a match must satisfy them; on ``node_absent`` a node
    only counts as "present" when it satisfies them.
    """

    name: str
    type: str | None = None
    fields_truthy: tuple[str, ...] = ()
    fields_falsy: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EdgeRef:
    """``directed=False`` accepts the edge in either direction -- for
    symmetric relations (knows, rival_of) where the model may reasonably
    anchor the link on either endpoint. An empty ``label`` accepts any
    label: "these two must be linked" without prescribing the word."""

    source: str
    label: str
    target: str
    directed: bool = True


@dataclass(frozen=True, slots=True)
class SeedNode:
    """``ref`` is the handle seed edges use to name this node; it defaults
    to ``name`` and only needs spelling out when two seeds share a display
    name (a duplicate-name world is a legitimate fixture).

    ``out_of_band`` stages the node in the backend but NOT the index -- a
    human created it in the Anytype UI after the bot's last sync; only a
    resync surfaces it. Such a node cannot anchor seed edges (it has no
    id until mid-trial) and cannot be ``stale``. It counts toward the
    ``node_count_delta`` baseline: it exists in the space from the start,
    so a trial that duplicates it shows up as an extra node.
    """

    type: str
    name: str
    summary: str
    ref: str = ""
    stale: bool = False
    out_of_band: bool = False
    story_time: float | str | None = None
    fields: Mapping[str, str] = field(default_factory=dict)
    body: str = ""
    icon: str = ""

    @property
    def handle(self) -> str:
        return self.ref or self.name


@dataclass(frozen=True, slots=True)
class SeedEdge:
    source: str
    label: str
    target: str


@dataclass(frozen=True, slots=True)
class GraphExpect:
    node_count_delta: int | None = None
    node_exists: tuple[NodeRef, ...] = ()
    node_absent: tuple[NodeRef, ...] = ()
    edge_exists: tuple[EdgeRef, ...] = ()
    no_stale_summaries: bool = False


@dataclass(frozen=True, slots=True)
class ToolExpect:
    called_any: tuple[str, ...] = ()
    not_executed: tuple[str, ...] = ()
    max_executed_calls: int | None = None
    max_decisions: int | None = None


@dataclass(frozen=True, slots=True)
class SessionExpect:
    working_set_holds: tuple[str, ...] = ()
    scratchpad_contains: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReplyExpect:
    must_mention: tuple[str, ...] = ()
    must_not_mention: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JudgeSpec:
    """``required=True`` gates the trial on the judge's verdict -- for
    expectations only a judge can catch (fabricated success, dishonest
    replies). A required judge runs on every live run, ``--judge`` or not;
    by default judge verdicts report alongside code grades without
    overruling them."""

    rubric: str
    required: bool = False


@dataclass(frozen=True, slots=True)
class CaseMode:
    """An in-space Activity Mode staged for this trial's registry.

    Mirrors the ModeStore payload shape (``modes._parse_in_space``): the
    display ``name`` slugifies to the ``/mode`` name, ``goal`` is the mode
    prompt. Lets a case reproduce custom or MISCONFIGURED space modes
    (e.g. a "task creation" mode someone forgot to make mutating).
    """

    name: str
    goal: str
    mutating: bool = False


@dataclass(frozen=True, slots=True)
class Turn:
    """One user message; ``seed_memory`` (first turn only) primes the
    conversation ring with reconstructed (kind, text) history."""

    user: str
    seed_memory: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    mode: str = ""  # "" = the registry default
    trials: int = 1
    provenance: bool = False  # opt-in: intent nodes perturb node counts
    must_fail: bool = False  # grader-honesty case: PASSING is the defect
    seed_nodes: tuple[SeedNode, ...] = ()
    seed_edges: tuple[SeedEdge, ...] = ()
    modes: tuple[CaseMode, ...] = ()  # extra in-space modes for the registry
    turns: tuple[Turn, ...] = ()
    graph: GraphExpect = field(default_factory=GraphExpect)
    tools: ToolExpect = field(default_factory=ToolExpect)
    session: SessionExpect = field(default_factory=SessionExpect)
    reply: ReplyExpect = field(default_factory=ReplyExpect)
    judge: JudgeSpec | None = None
    script: tuple[LLMTurn, ...] = ()


@dataclass(frozen=True, slots=True)
class EvalSuite:
    name: str
    profile: str
    embedder: str  # GC_EMBEDDER for this suite: off | hash (deterministic)
    cases: tuple[EvalCase, ...]
    source: Path


_SUITE_KEYS = {"name", "profile", "embedder"}
_CASE_KEYS = {
    "id", "mode", "trials", "provenance", "must_fail",
    "seed", "modes", "turn", "expect", "judge", "script",
}
_MODE_KEYS = {"name", "goal", "mutating"}  # no capture until a case needs it
_SEED_KEYS = {"node", "edge"}
_SEED_NODE_KEYS = {
    "type", "name", "summary", "stale", "out_of_band", "story_time",
    "fields", "body", "icon", "ref",
}
_SEED_EDGE_KEYS = {"source", "label", "target"}
_TURN_KEYS = {"user", "seed_memory"}
_EXPECT_KEYS = {"graph", "tools", "session", "reply"}
_GRAPH_KEYS = {
    "node_count_delta", "node_exists", "node_absent", "edge_exists", "no_stale_summaries",
}
_NODE_REF_KEYS = {"name", "type", "fields_truthy", "fields_falsy"}
_TOOLS_KEYS = {"called_any", "not_executed", "max_executed_calls", "max_decisions"}
_SESSION_KEYS = {"working_set_holds", "scratchpad_contains"}
_REPLY_KEYS = {"must_mention", "must_not_mention"}
_JUDGE_KEYS = {"rubric", "required"}
_SCRIPT_KEYS = {"reply", "tool_calls"}
_TOOL_CALL_KEYS = {"name", "arguments"}
_EMBEDDERS = {"off", "hash"}  # "local" downloads a model; evals stay deterministic


def load_suites(path: str | Path) -> list[EvalSuite]:
    """Every suite at ``path``: one file, or every ``*.toml`` in a directory."""
    root = Path(path)
    if root.is_dir():
        files = sorted(root.glob("*.toml"))
        if not files:
            raise DatasetError(f"{root}: no *.toml case files found")
        return [load_suite(file) for file in files]
    return [load_suite(root)]


def load_suite(path: Path) -> EvalSuite:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise DatasetError(f"case file not found: {path}") from None
    except tomllib.TOMLDecodeError as err:
        raise DatasetError(f"{path} is not valid TOML: {err}") from None
    suite = _table(data.get("suite"), f"{path} [suite]")
    _no_unknown(suite, _SUITE_KEYS, f"{path} [suite]")
    name = _required_str(suite, "name", f"{path} [suite]")
    embedder = str(suite.get("embedder", "off"))
    if embedder not in _EMBEDDERS:
        raise DatasetError(
            f"{path} [suite]: embedder {embedder!r} not allowed; "
            f"allowed: {sorted(_EMBEDDERS)}"
        )
    raw_cases = data.get("case")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise DatasetError(f"{path} must define at least one [[case]]")
    unknown_top = set(data) - {"suite", "case"}
    if unknown_top:
        raise DatasetError(
            f"{path} has unknown top-level keys {sorted(unknown_top)}; "
            "allowed: suite, case"
        )
    cases = []
    seen_ids: set[str] = set()
    for raw in raw_cases:
        case = _parse_case(_table(raw, f"{path} [[case]]"), path)
        if case.id in seen_ids:
            raise DatasetError(f"{path}: duplicate case id {case.id!r}")
        seen_ids.add(case.id)
        cases.append(case)
    return EvalSuite(
        name=name,
        profile=_required_str(suite, "profile", f"{path} [suite]"),
        embedder=embedder,
        cases=tuple(cases),
        source=path,
    )


def _parse_case(raw: Mapping[str, Any], path: Path) -> EvalCase:
    case_id = _required_str(raw, "id", f"{path} [[case]]")
    origin = f"{path} case {case_id!r}"
    _no_unknown(raw, _CASE_KEYS, origin)
    turns = tuple(
        _parse_turn(_table(t, f"{origin} [[case.turn]]"), i, origin)
        for i, t in enumerate(_list(raw.get("turn", []), f"{origin}: turn"))
    )
    if not turns:
        raise DatasetError(f"{origin}: at least one [[case.turn]] is required")
    seed = _table(raw.get("seed", {}), f"{origin} [case.seed]")
    _no_unknown(seed, _SEED_KEYS, f"{origin} [case.seed]")
    expect = _table(raw.get("expect", {}), f"{origin} [case.expect]")
    _no_unknown(expect, _EXPECT_KEYS, f"{origin} [case.expect]")
    judge = None
    if raw.get("judge") is not None:
        judge_raw = _table(raw["judge"], f"{origin} [case.judge]")
        _no_unknown(judge_raw, _JUDGE_KEYS, f"{origin} [case.judge]")
        judge = JudgeSpec(
            rubric=_required_str(judge_raw, "rubric", f"{origin} [case.judge]"),
            required=_flag(
                judge_raw.get("required", False), f"{origin} [case.judge]: required"
            ),
        )
    return EvalCase(
        id=case_id,
        mode=str(raw.get("mode", "")),
        trials=_positive_int(raw.get("trials", 1), f"{origin}: trials"),
        provenance=_flag(raw.get("provenance", False), f"{origin}: provenance"),
        must_fail=_flag(raw.get("must_fail", False), f"{origin}: must_fail"),
        seed_nodes=tuple(
            _parse_seed_node(_table(n, f"{origin} [[case.seed.node]]"), origin)
            for n in _list(seed.get("node", []), f"{origin}: seed.node")
        ),
        seed_edges=tuple(
            _parse_seed_edge(_table(e, f"{origin} [[case.seed.edge]]"), origin)
            for e in _list(seed.get("edge", []), f"{origin}: seed.edge")
        ),
        modes=tuple(
            _parse_case_mode(_table(m, f"{origin} [[case.modes]]"), origin)
            for m in _list(raw.get("modes", []), f"{origin}: modes")
        ),
        turns=turns,
        graph=_parse_graph(
            _table(expect.get("graph", {}), f"{origin} [case.expect.graph]"), origin
        ),
        tools=_parse_tools(
            _table(expect.get("tools", {}), f"{origin} [case.expect.tools]"), origin
        ),
        session=_parse_session(
            _table(expect.get("session", {}), f"{origin} [case.expect.session]"), origin
        ),
        reply=_parse_reply(
            _table(expect.get("reply", {}), f"{origin} [case.expect.reply]"), origin
        ),
        judge=judge,
        script=tuple(
            _parse_script_turn(_table(s, f"{origin} [[case.script]]"), origin)
            for s in _list(raw.get("script", []), f"{origin}: script")
        ),
    )


def _parse_seed_node(raw: Mapping[str, Any], origin: str) -> SeedNode:
    ctx = f"{origin} [[case.seed.node]]"
    _no_unknown(raw, _SEED_NODE_KEYS, ctx)
    fields = _table(raw.get("fields", {}), f"{ctx}: fields")
    story_time = raw.get("story_time")
    if story_time is not None and not isinstance(story_time, int | float | str):
        raise DatasetError(f"{ctx}: story_time must be a number or string")
    stale = _flag(raw.get("stale", False), f"{ctx}: stale")
    out_of_band = _flag(raw.get("out_of_band", False), f"{ctx}: out_of_band")
    if stale and out_of_band:
        raise DatasetError(
            f"{ctx}: out_of_band and stale cannot combine (staleness is "
            "index state; an out-of-band node is not in the index yet)"
        )
    return SeedNode(
        type=_required_str(raw, "type", ctx),
        name=_required_str(raw, "name", ctx),
        summary=_required_str(raw, "summary", ctx),
        ref=str(raw.get("ref", "")),
        stale=stale,
        out_of_band=out_of_band,
        story_time=story_time,
        fields={str(k): str(v) for k, v in fields.items()},
        body=str(raw.get("body", "")),
        icon=str(raw.get("icon", "")),
    )


def _parse_seed_edge(raw: Mapping[str, Any], origin: str) -> SeedEdge:
    ctx = f"{origin} [[case.seed.edge]]"
    _no_unknown(raw, _SEED_EDGE_KEYS, ctx)
    return SeedEdge(
        source=_required_str(raw, "source", ctx),
        label=_required_str(raw, "label", ctx),
        target=_required_str(raw, "target", ctx),
    )


def _parse_case_mode(raw: Mapping[str, Any], origin: str) -> CaseMode:
    """Shape validation only; slugification and duplicate detection stay in
    ``modes._parse_in_space`` (the single validation seam for mode config),
    reached when the runner builds the trial registry."""
    ctx = f"{origin} [[case.modes]]"
    _no_unknown(raw, _MODE_KEYS, ctx)
    return CaseMode(
        name=_required_str(raw, "name", ctx),
        goal=_required_str(raw, "goal", ctx),
        mutating=_flag(raw.get("mutating", False), f"{ctx}: mutating"),
    )


def _parse_turn(raw: Mapping[str, Any], index: int, origin: str) -> Turn:
    ctx = f"{origin} [[case.turn]] #{index + 1}"
    _no_unknown(raw, _TURN_KEYS, ctx)
    memory = []
    for pair in _list(raw.get("seed_memory", []), f"{ctx}: seed_memory"):
        if (
            not isinstance(pair, list) or len(pair) != 2
            or pair[0] not in ("user", "assistant")
        ):
            raise DatasetError(
                f'{ctx}: seed_memory entries are ["user"|"assistant", "text"] pairs'
            )
        memory.append((str(pair[0]), str(pair[1])))
    if memory and index != 0:
        raise DatasetError(f"{ctx}: seed_memory belongs on the first turn only")
    return Turn(user=_required_str(raw, "user", ctx), seed_memory=tuple(memory))


def _parse_graph(raw: Mapping[str, Any], origin: str) -> GraphExpect:
    ctx = f"{origin} [case.expect.graph]"
    _no_unknown(raw, _GRAPH_KEYS, ctx)
    delta = raw.get("node_count_delta")
    if delta is not None and (isinstance(delta, bool) or not isinstance(delta, int)):
        raise DatasetError(f"{ctx}: node_count_delta must be an integer")
    return GraphExpect(
        node_count_delta=delta,
        node_exists=_node_refs(raw.get("node_exists", []), f"{ctx}: node_exists"),
        node_absent=_node_refs(raw.get("node_absent", []), f"{ctx}: node_absent"),
        edge_exists=tuple(
            _parse_edge_ref(e, f"{ctx}: edge_exists")
            for e in _tables(raw.get("edge_exists", []), f"{ctx}: edge_exists")
        ),
        no_stale_summaries=_flag(
            raw.get("no_stale_summaries", False), f"{ctx}: no_stale_summaries"
        ),
    )


_EDGE_REF_KEYS = {"source", "label", "target", "directed"}


def _parse_edge_ref(raw: Mapping[str, Any], origin: str) -> EdgeRef:
    _no_unknown(raw, _EDGE_REF_KEYS, origin)
    return EdgeRef(
        source=_required_str(raw, "source", origin),
        label=str(raw.get("label", "")),
        target=_required_str(raw, "target", origin),
        directed=_flag(raw.get("directed", True), f"{origin}: directed"),
    )


def _parse_tools(raw: Mapping[str, Any], origin: str) -> ToolExpect:
    ctx = f"{origin} [case.expect.tools]"
    _no_unknown(raw, _TOOLS_KEYS, ctx)
    return ToolExpect(
        called_any=_str_tuple(raw.get("called_any", []), f"{ctx}: called_any"),
        not_executed=_str_tuple(raw.get("not_executed", []), f"{ctx}: not_executed"),
        max_executed_calls=_optional_positive(
            raw.get("max_executed_calls"), f"{ctx}: max_executed_calls"
        ),
        max_decisions=_optional_positive(
            raw.get("max_decisions"), f"{ctx}: max_decisions"
        ),
    )


def _parse_session(raw: Mapping[str, Any], origin: str) -> SessionExpect:
    ctx = f"{origin} [case.expect.session]"
    _no_unknown(raw, _SESSION_KEYS, ctx)
    return SessionExpect(
        working_set_holds=_str_tuple(
            raw.get("working_set_holds", []), f"{ctx}: working_set_holds"
        ),
        scratchpad_contains=_str_tuple(
            raw.get("scratchpad_contains", []), f"{ctx}: scratchpad_contains"
        ),
    )


def _parse_reply(raw: Mapping[str, Any], origin: str) -> ReplyExpect:
    ctx = f"{origin} [case.expect.reply]"
    _no_unknown(raw, _REPLY_KEYS, ctx)
    return ReplyExpect(
        must_mention=_str_tuple(raw.get("must_mention", []), f"{ctx}: must_mention"),
        must_not_mention=_str_tuple(
            raw.get("must_not_mention", []), f"{ctx}: must_not_mention"
        ),
    )


def _parse_script_turn(raw: Mapping[str, Any], origin: str) -> LLMTurn:
    ctx = f"{origin} [[case.script]]"
    _no_unknown(raw, _SCRIPT_KEYS, ctx)
    calls = []
    for call in _tables(raw.get("tool_calls", []), f"{ctx}: tool_calls"):
        _no_unknown(call, _TOOL_CALL_KEYS, f"{ctx}: tool_calls")
        calls.append(ToolCall(
            name=_required_str(call, "name", f"{ctx}: tool_calls"),
            arguments=dict(_table(call.get("arguments", {}), f"{ctx}: arguments")),
        ))
    reply = str(raw.get("reply", ""))
    if not calls and not reply:
        raise DatasetError(f"{ctx}: a script step needs tool_calls or a reply")
    return LLMTurn(reply=reply, tool_calls=tuple(calls))


# -- shared low-level validators ------------------------------------------


def _table(value: Any, origin: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise DatasetError(f"{origin} must be a table")
    return value


def _tables(value: Any, origin: str) -> list[Mapping[str, Any]]:
    return [_table(item, origin) for item in _list(value, origin)]


def _list(value: Any, origin: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise DatasetError(f"{origin} must be a list")
    return value


def _no_unknown(raw: Mapping[str, Any], allowed: set[str], origin: str) -> None:
    unknown = set(raw) - allowed
    if unknown:
        raise DatasetError(
            f"{origin} has unknown keys {sorted(unknown)}; allowed: {sorted(allowed)}"
        )


def _required_str(raw: Mapping[str, Any], key: str, origin: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{origin}: {key!r} must be a non-empty string")
    return value


def _str_tuple(value: Any, origin: str) -> tuple[str, ...]:
    return tuple(str(item) for item in _list(value, origin))


def _node_refs(value: Any, origin: str) -> tuple[NodeRef, ...]:
    refs = []
    for raw in _tables(value, origin):
        _no_unknown(raw, _NODE_REF_KEYS, origin)
        raw_type = raw.get("type")
        truthy = _str_tuple(raw.get("fields_truthy", []), f"{origin}: fields_truthy")
        falsy = _str_tuple(raw.get("fields_falsy", []), f"{origin}: fields_falsy")
        for label, keys in (("fields_truthy", truthy), ("fields_falsy", falsy)):
            if any(not key.strip() for key in keys):
                raise DatasetError(f"{origin}: {label} keys must be non-empty")
        overlap = set(truthy) & set(falsy)
        if overlap:
            raise DatasetError(
                f"{origin}: keys {sorted(overlap)} appear in both "
                "fields_truthy and fields_falsy"
            )
        refs.append(NodeRef(
            name=_required_str(raw, "name", origin),
            type=str(raw_type) if raw_type is not None else None,
            fields_truthy=truthy,
            fields_falsy=falsy,
        ))
    return tuple(refs)


def _flag(value: Any, origin: str) -> bool:
    if not isinstance(value, bool):
        raise DatasetError(f"{origin} must be true or false")
    return value


def _positive_int(value: Any, origin: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DatasetError(f"{origin} must be a positive integer, got {value!r}")
    return int(value)


def _optional_positive(value: Any, origin: str) -> int | None:
    if value is None:
        return None
    return _positive_int(value, origin)
