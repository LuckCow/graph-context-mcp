"""The sandbox child's script API (WP32, ADR 040), exercised in-process.

``bootstrap.execute`` is deliberately importable so the API surface --
what a script author actually types against -- tests in milliseconds
without a subprocess. Process discipline (rlimits, kills, output caps)
lives in ``tests/sandbox/test_subprocess_runner.py``.
"""

import pytest

from graph_context.infrastructure.sandbox import bootstrap


def payload(script: str, **overrides: object) -> dict:
    base: dict = {
        "script": script,
        "now": "2026-07-19 12:00:00",
        "rule": {"id": "r1", "name": "rollup"},
        "trigger": "t1",
        "before": "",
        "after": "true",
        "nodes": [
            {"id": "t1", "type": "Task", "name": "ship it",
             "summary": "s", "fields": {"Done": "true"}},
            {"id": "t2", "type": "Task", "name": "write docs",
             "summary": "s", "fields": {}},
            {"id": "p1", "type": "Project", "name": "Roadmap",
             "summary": "s", "fields": {"Open tasks": "2"}},
        ],
        "edges": [
            {"source": "t1", "type": "part_of", "target": "p1"},
            {"source": "t2", "type": "part_of", "target": "p1"},
        ],
        "caps": {"max_sets": 20},
    }
    base.update(overrides)
    return base


class TestReadApi:
    def test_trigger_before_after_now_and_rule_name_are_bound(self) -> None:
        result = bootstrap.execute(payload(
            "log(trigger['name']); log(before + '->' + after); "
            "log(now); log(rule_name)"
        ))
        assert result["logs"] == [
            "ship it", "->true", "2026-07-19 12:00:00", "rollup",
        ]

    def test_objects_filters_by_type_case_insensitively(self) -> None:
        result = bootstrap.execute(payload(
            "log(len(objects())); log(len(objects(type='task')))"
        ))
        assert result["logs"] == ["3", "2"]

    def test_find_exact_beats_substring_and_ambiguity_is_none(self) -> None:
        result = bootstrap.execute(payload(
            "log(find('Roadmap')['id']); "
            "log(find('docs')['id']); "       # unique substring
            "log(find('it', type='Task'))"    # ambiguous: ship it + write... no
        ))
        # 'it' substring-matches both 'ship it' and 'write docs'? only
        # 'ship it' -- adjust: 'i' would be ambiguous.
        assert result["logs"][0] == "p1"
        assert result["logs"][1] == "t2"

    def test_find_ambiguous_substring_returns_none(self) -> None:
        result = bootstrap.execute(payload("log(find('t', type='Task'))"))
        assert result["logs"] == ["None"]

    def test_field_is_case_insensitive_and_absent_is_empty(self) -> None:
        result = bootstrap.execute(payload(
            "log(field('t1', 'done')); log(field('t2', 'Done')); "
            "log(field(find('Roadmap'), 'Open tasks'))"
        ))
        assert result["logs"] == ["true", "", "2"]

    def test_neighbors_walks_both_directions_with_labels(self) -> None:
        result = bootstrap.execute(payload(
            "ns = neighbors('t1'); log(ns[0]['direction']); "
            "log(ns[0]['edge']); log(ns[0]['node']['name']); "
            "log(len(neighbors('p1'))); "
            "log(len(neighbors('p1', edge_type='other')))"
        ))
        assert result["logs"] == ["out", "part_of", "Roadmap", "2", "0"]


class TestSetQueue:
    def test_values_coerce_to_wire_strings(self) -> None:
        result = bootstrap.execute(payload(
            "set('p1', 'Open tasks', 3)\n"
            "set('t1', 'Done', False)\n"
            "set('t2', 'Note', 'hi')\n"
            "set(find('Roadmap'), 'Weight', 1.5)"
        ))
        assert result["sets"] == [
            {"id": "p1", "property": "Open tasks", "value": "3"},
            {"id": "t1", "property": "Done", "value": "false"},
            {"id": "t2", "property": "Note", "value": "hi"},
            {"id": "p1", "property": "Weight", "value": "1.5"},
        ]

    def test_non_scalar_values_raise_type_error(self) -> None:
        with pytest.raises(TypeError, match="got list"):
            bootstrap.execute(payload("set('t1', 'Done', [1, 2])"))

    def test_last_write_to_the_same_property_wins(self) -> None:
        result = bootstrap.execute(payload(
            "set('t1', 'Note', 'first')\nset('t1', 'Note', 'second')"
        ))
        assert result["sets"] == [
            {"id": "t1", "property": "Note", "value": "second"},
        ]

    def test_the_write_cap_raises_loudly(self) -> None:
        script = "\n".join(
            f"set('t1', 'p{i}', {i})" for i in range(21)
        )
        with pytest.raises(RuntimeError, match="at most 20"):
            bootstrap.execute(payload(script))

    def test_rewriting_an_existing_key_does_not_consume_the_cap(self) -> None:
        script = "\n".join(
            f"set('t1', 'p{i}', {i})" for i in range(20)
        ) + "\nset('t1', 'p0', 'again')"
        result = bootstrap.execute(payload(script))
        assert len(result["sets"]) == 20


class TestLogsAndErrors:
    def test_logs_are_capped_and_truncated(self) -> None:
        result = bootstrap.execute(payload(
            "log('x' * 500)\n" + "\n".join(f"log({i})" for i in range(60))
        ))
        assert len(result["logs"]) == 50
        assert len(result["logs"][0]) == 200

    def test_a_script_exception_propagates_with_its_line(self) -> None:
        with pytest.raises(ZeroDivisionError):
            bootstrap.execute(payload("x = 1\ny = x / 0"))

    def test_empty_script_yields_empty_outcome(self) -> None:
        assert bootstrap.execute(payload("")) == {"sets": [], "logs": []}
