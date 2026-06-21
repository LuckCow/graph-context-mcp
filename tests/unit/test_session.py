"""Focus stack and recent-history behaviour."""

from graph_context.domain.session import FocusStack, RecentHistory


class TestFocusStack:
    def test_push_moves_existing_to_top_without_duplicating(self) -> None:
        stack = FocusStack()
        for node_id in ("a", "b", "a"):
            stack.push(node_id)
        assert [e.node_id for e in stack.entries] == ["a", "b"]

    def test_overflow_evicts_oldest_unpinned(self) -> None:
        stack = FocusStack(max_size=3)
        stack.push("keep")
        stack.pin("keep")
        for node_id in ("b", "c", "d"):
            stack.push(node_id)
        ids = [e.node_id for e in stack.entries]
        assert "keep" in ids and "b" not in ids and len(stack) == 3

    def test_all_pinned_allows_temporary_overflow(self) -> None:
        stack = FocusStack(max_size=2)
        for node_id in ("a", "b"):
            stack.push(node_id)
            stack.pin(node_id)
        stack.push("c")
        assert len(stack) == 3

    def test_clear_keeps_pinned_by_default(self) -> None:
        stack = FocusStack()
        stack.push("a")
        stack.push("b")
        stack.pin("b")
        stack.clear()
        assert [e.node_id for e in stack.entries] == ["b"]


class TestRecentHistory:
    def test_skips_consecutive_duplicates_and_orders_recent_first(self) -> None:
        recent = RecentHistory(max_size=3)
        for node_id in ("a", "a", "b", "c", "d"):
            recent.record(node_id)
        assert recent.items == ("d", "c", "b")
