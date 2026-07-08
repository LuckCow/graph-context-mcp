"""Working-set buckets, recent-history, and snapshot behaviour (WP15)."""

import pytest

from graph_context.domain.models import Detail
from graph_context.domain.session import (
    RecentHistory,
    SessionState,
    WorkingSet,
    WorkingSetEntry,
)


class TestWorkingSetHold:
    def test_hold_moves_existing_to_top_without_duplicating(self) -> None:
        ws = WorkingSet()
        for node_id in ("a", "b", "a"):
            ws.hold(node_id)
        assert [e.node_id for e in ws.entries] == ["a", "b"]

    def test_re_holding_re_levels_the_entry(self) -> None:
        ws = WorkingSet()
        ws.hold("a", Detail.SUMMARIES)
        ws.hold("a", Detail.FULL)
        assert ws.entries == (WorkingSetEntry("a", Detail.FULL),)

    def test_names_is_not_a_holdable_level(self) -> None:
        with pytest.raises(ValueError):
            WorkingSet().hold("a", Detail.NAMES)

    def test_top_is_the_most_recently_held(self) -> None:
        ws = WorkingSet()
        ws.hold("a")
        ws.hold("b")
        assert ws.top == "b"

    def test_release_and_clear(self) -> None:
        ws = WorkingSet()
        ws.hold("a")
        assert ws.release("a") is True
        assert ws.release("a") is False  # already gone
        ws.hold("b")
        ws.clear()
        assert ws.entries == ()


class TestWorkingSetBucketCaps:
    def test_third_full_hold_demotes_the_oldest_full_entry(self) -> None:
        ws = WorkingSet(full_slots=2)
        for node_id in ("a", "b"):
            ws.hold(node_id, Detail.FULL)
        outcome = ws.hold("c", Detail.FULL)
        assert outcome.demoted == ("a",)
        assert {e.node_id: e.detail for e in ws.entries} == {
            "c": Detail.FULL, "b": Detail.FULL, "a": Detail.SUMMARIES,
        }

    def test_summary_overflow_evicts_the_oldest_summary_entry(self) -> None:
        ws = WorkingSet(summary_slots=2)
        for node_id in ("a", "b", "c"):
            outcome = ws.hold(node_id)
        assert outcome.evicted == ("a",)
        assert [e.node_id for e in ws.entries] == ["c", "b"]

    def test_demotion_can_cascade_into_a_summary_eviction(self) -> None:
        ws = WorkingSet(full_slots=1, summary_slots=1)
        ws.hold("old-summary")
        ws.hold("old-full", Detail.FULL)
        outcome = ws.hold("new-full", Detail.FULL)
        # old-full demotes out of the full bucket, overflowing the summary
        # bucket, which evicts its oldest entry.
        assert outcome.demoted == ("old-full",)
        assert outcome.evicted == ("old-summary",)
        assert [e.node_id for e in ws.entries] == ["new-full", "old-full"]


class TestRecentHistory:
    def test_skips_consecutive_duplicates_and_orders_recent_first(self) -> None:
        recent = RecentHistory(max_size=3)
        for node_id in ("a", "a", "b", "c", "d"):
            recent.record(node_id)
        assert recent.items == ("d", "c", "b")

    def test_re_recording_moves_to_front_without_duplicating(self) -> None:
        # The composite-create wart: touching the new node, then a link
        # target that was already recent, must not leave "siege, mira, siege".
        recent = RecentHistory(max_size=6)
        for node_id in ("mira", "siege", "mira"):
            recent.record(node_id)
        assert recent.items == ("mira", "siege")  # distinct, most-recent first


class TestSessionDefaults:
    def test_touch_records_recent_but_never_holds(self) -> None:
        session = SessionState()
        session.touch("a")
        assert session.recent.items == ("a",)
        assert session.working_set.entries == ()

    def test_default_start_prefers_held_over_touched(self) -> None:
        session = SessionState()
        session.touch("touched")
        assert session.default_start() == "touched"
        session.working_set.hold("held")
        assert session.default_start() == "held"

    def test_default_start_is_none_when_nothing_happened(self) -> None:
        assert SessionState().default_start() is None


class TestSnapshot:
    def test_v2_round_trip_keeps_scratchpad_details_and_mode(self) -> None:
        session = SessionState(
            project="Ashfall", scratchpad="finish the siege arc", mode="authoring"
        )
        session.working_set.hold("a", Detail.FULL)
        session.working_set.hold("b")
        session.touch("c")
        restored = SessionState.from_snapshot(session.to_snapshot())
        assert restored.project == "Ashfall"
        assert restored.scratchpad == "finish the siege arc"
        assert restored.mode == "authoring"
        assert restored.working_set.entries == session.working_set.entries
        assert restored.recent.items == ("c",)

    def test_missing_or_junk_mode_restores_empty(self) -> None:
        assert SessionState.from_snapshot({"version": 2}).mode == ""
        assert SessionState.from_snapshot({"mode": ["not", "a", "str"]}).mode == ""

    def test_v1_focus_entries_restore_as_summary_holds(self) -> None:
        v1 = {
            "version": 1,
            "project": "Ashfall",
            "focus": [
                {"node_id": "a", "pinned": True},
                {"node_id": "b", "pinned": False},
            ],
            "recent": ["b", "a"],
        }
        restored = SessionState.from_snapshot(v1)
        assert restored.working_set.entries == (
            WorkingSetEntry("a", Detail.SUMMARIES),
            WorkingSetEntry("b", Detail.SUMMARIES),
        )
        assert restored.scratchpad == ""

    def test_corrupt_fields_degrade_instead_of_crashing(self) -> None:
        snapshot = {
            "version": 2,
            "scratchpad": ["not", "a", "string"],
            "working_set": [
                {"detail": "full"},                    # id-less: dropped
                {"node_id": "a", "detail": "bogus"},   # unknown level: summaries
                {"node_id": "b", "detail": "names"},   # unholdable: summaries
            ],
            "recent": ["r1", "r2"],
        }
        restored = SessionState.from_snapshot(snapshot)
        assert restored.scratchpad == ""
        assert restored.working_set.entries == (
            WorkingSetEntry("a", Detail.SUMMARIES),
            WorkingSetEntry("b", Detail.SUMMARIES),
        )
        assert restored.recent.items == ("r1", "r2")

    def test_restore_re_caps_an_oversized_snapshot(self) -> None:
        oversized = [
            {"node_id": f"f{i}", "detail": "full"} for i in range(4)
        ] + [
            {"node_id": f"s{i}", "detail": "summaries"} for i in range(8)
        ]
        restored = SessionState.from_snapshot(
            {"version": 2, "working_set": oversized, "recent": []}
        )
        details = [e.detail for e in restored.working_set.entries]
        assert details.count(Detail.FULL) == 2
        assert details.count(Detail.SUMMARIES) == 6
