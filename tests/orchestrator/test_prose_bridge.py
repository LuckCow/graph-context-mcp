"""The prose bridge (WP43): payload builders over a real historian, and
the thread/loop contract -- every call runs ON the owning loop, marks
take the route lock, timeouts surface as TimeoutError (the server's
504)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator

import pytest

from graph_context.application.node_historian import NodeHistorian
from graph_context.domain import revisions
from graph_context.domain.models import NodeDraft
from graph_context.infrastructure.memory.fake_repository import (
    InMemoryGraphRepository,
)
from graph_context.orchestrator.prose_bridge import (
    ProseBridge,
    ProseSpace,
    register_space,
)

P1 = "The city fell quiet before the siege began, every gate barred."
P2 = "Mira counted the engines twice; one was missing from the yard."
P3 = "Rain came at dusk and the watch fires guttered along the wall."
# A human's light touch on P2 -- similar enough for diff lineage.
P2_EDIT = "Mira counted the engines three times; one was missing from the yard."


def _h(text: str) -> str:
    return revisions.block_hash(revisions.normalize_block(text))


class SpyLock(asyncio.Lock):
    def __init__(self) -> None:
        super().__init__()
        self.acquisitions = 0

    async def acquire(self) -> bool:
        self.acquisitions += 1
        return await super().acquire()


@pytest.fixture
def loop() -> Iterator[asyncio.AbstractEventLoop]:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


@pytest.fixture
def world(loop) -> tuple[ProseBridge, str, SpyLock]:
    """One registered space with a revised, human-edited chapter."""
    bridge = ProseBridge()
    lock = SpyLock()

    async def build() -> str:
        repo = InMemoryGraphRepository()
        await repo.create_node(NodeDraft(
            type="gc_space_context", name="Space Context", summary="cfg",
            fields={revisions.FIELD_TRACKED_TYPES: "Chapter"},
        ))
        node = await repo.create_node(NodeDraft(
            type="Chapter", name="Chapter One", summary="ch",
            body=f"{P1}\n\n{P2}",
        ))
        historian = NodeHistorian(repo, now=lambda: "T1")
        await historian.record_bot_revision(
            node.id, author_detail="fable · prose · u1",
        )
        await repo.update_node(node.id, body=f"{P1}\n\n{P2_EDIT}")
        await historian.record_external_revision(node.id)
        register_space(
            bridge, space_id="sp1", label="Ashfall",
            historian=historian, repository=repo, route_lock=lock,
        )
        return node.id

    node_id = asyncio.run_coroutine_threadsafe(build(), loop).result(10)
    return bridge, node_id, lock


class TestPayloads:
    def test_list_nodes_carries_the_revision_roll_up(self, world) -> None:
        bridge, node_id, _ = world
        (row,) = bridge.call("sp1", "list_nodes")
        assert row["id"] == node_id
        assert row["name"] == "Chapter One"
        assert row["revisions"] == 2
        assert row["last_author"] == "human"

    def test_node_view_joins_blocks_blame_and_state(self, world) -> None:
        bridge, node_id, _ = world
        view = bridge.call("sp1", "node_view", node_id)
        assert view["name"] == "Chapter One"
        by_hash = {b["hash"]: b for b in view["blocks"]}
        assert by_hash[_h(P1)]["blame"]["author"] == "model"
        assert by_hash[_h(P2_EDIT)]["blame"]["author"] == "human"
        # Status FOLLOWS the ancestor across a human edit (WP42): the
        # edited block inherits raw_ai -- "human" is for brand-new blocks.
        assert by_hash[_h(P2_EDIT)]["status"] == "raw_ai"
        assert by_hash[_h(P1)]["intent"] == "flexible"
        assert [r["seq"] for r in view["revisions"]] == [1, 2]
        assert view["revisions"][1]["added"] == 1

    def test_diff_pairs_the_replacement_with_word_spans(self, world) -> None:
        bridge, node_id, _ = world
        diff = bridge.call("sp1", "revision_diff", node_id, 2)
        (pair,) = diff["pairs"]
        assert pair["old_hash"] == _h(P2)
        assert pair["new_hash"] == _h(P2_EDIT)
        kinds = {kind for kind, _ in pair["spans"]}
        assert "add" in kinds and "del" in kinds

    def test_set_mark_folds_and_holds_the_route_lock(self, world) -> None:
        bridge, node_id, lock = world
        result = bridge.call(
            "sp1", "set_mark", node_id, _h(P1), "intent", "locked",
        )
        assert result == {
            "hash": _h(P1), "status": "raw_ai", "intent": "locked",
        }
        assert lock.acquisitions == 1

    def test_unknown_space_is_a_key_error(self, world) -> None:
        bridge, _, _ = world
        with pytest.raises(KeyError):
            bridge.call("nope", "list_nodes")


class TestThreadContract:
    def test_calls_run_on_the_owning_loop_thread(self, loop) -> None:
        bridge = ProseBridge()
        seen: list[int] = []

        async def where() -> list[dict[str, str]]:
            seen.append(threading.get_ident())
            return []

        async def register() -> int:
            bridge.register(ProseSpace(
                space_id="sp1", label="x",
                loop=asyncio.get_running_loop(),
                list_nodes=where, node_view=None,  # type: ignore[arg-type]
                revision_diff=None,  # type: ignore[arg-type]
                set_mark=None,  # type: ignore[arg-type]
            ))
            return threading.get_ident()

        loop_thread = asyncio.run_coroutine_threadsafe(
            register(), loop
        ).result(10)
        bridge.call("sp1", "list_nodes")
        assert seen == [loop_thread]
        assert loop_thread != threading.get_ident()

    def test_a_busy_loop_times_out_instead_of_hanging(self, loop) -> None:
        bridge = ProseBridge()

        async def slow() -> list[dict[str, str]]:
            await asyncio.sleep(30)
            return []

        async def register() -> None:
            bridge.register(ProseSpace(
                space_id="sp1", label="x",
                loop=asyncio.get_running_loop(),
                list_nodes=slow, node_view=None,  # type: ignore[arg-type]
                revision_diff=None,  # type: ignore[arg-type]
                set_mark=None,  # type: ignore[arg-type]
            ))

        asyncio.run_coroutine_threadsafe(register(), loop).result(10)
        with pytest.raises(TimeoutError):
            bridge.call("sp1", "list_nodes", timeout=0.05)
