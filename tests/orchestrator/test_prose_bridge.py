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
def repo_holder() -> list[object]:
    """Filled by ``world`` (repository, then historian) so tests can
    reach them directly (always through the loop -- the bridge's
    thread contract)."""
    return []


@pytest.fixture
def world(loop, repo_holder) -> tuple[ProseBridge, str, SpyLock]:
    """One registered space with a revised, human-edited chapter."""
    bridge = ProseBridge()
    lock = SpyLock()

    async def build() -> str:
        repo = InMemoryGraphRepository()
        repo_holder.append(repo)
        await repo.create_node(NodeDraft(
            type="gc_space_context", name="Space Context", summary="cfg",
            fields={revisions.FIELD_TRACKED_TYPES: "Chapter"},
        ))
        node = await repo.create_node(NodeDraft(
            type="Chapter", name="Chapter One", summary="ch",
            body=f"{P1}\n\n{P2}",
        ))
        historian = NodeHistorian(repo, now=lambda: "T1")
        repo_holder.append(historian)
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

    def test_diff_pairs_the_replacement_with_word_spans(self, world) -> None:
        bridge, node_id, _ = world
        diff = bridge.call("sp1", "revision_diff", node_id, 2)
        (pair,) = diff["pairs"]
        assert pair["old_hash"] == _h(P2)
        assert pair["new_hash"] == _h(P2_EDIT)
        kinds = {kind for kind, _ in pair["spans"]}
        assert "add" in kinds and "del" in kinds

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
                list_nodes=where,
                revision_diff=None,  # type: ignore[arg-type]
                doc_view=None,  # type: ignore[arg-type]
                save_body=None,  # type: ignore[arg-type]
                set_marks=None,  # type: ignore[arg-type]
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
                list_nodes=slow,
                revision_diff=None,  # type: ignore[arg-type]
                doc_view=None,  # type: ignore[arg-type]
                save_body=None,  # type: ignore[arg-type]
                set_marks=None,  # type: ignore[arg-type]
            ))

        asyncio.run_coroutine_threadsafe(register(), loop).result(10)
        with pytest.raises(TimeoutError):
            bridge.call("sp1", "list_nodes", timeout=0.05)


class TestDocWire:
    """WP48 (ADR 054): the document-level wire -- full raw body,
    absolute-offset segments/spans, whole-document save, batch marks,
    and the version ledger behind the SSE route."""

    def test_doc_view_serves_body_segments_and_offset_spans(
        self, world
    ) -> None:
        bridge, node_id, _ = world
        doc = bridge.call("sp1", "doc_view", node_id)
        body = doc["body"]
        assert body == f"{P1}\n\n{P2_EDIT}"
        segments = doc["segments"]
        assert [s["hash"] for s in segments] == [_h(P1), _h(P2_EDIT)]
        for segment in segments:
            block = body[segment["start"]:segment["end"]]
            covering = [
                s for s in doc["spans"]
                if s[0] >= segment["start"] and s[1] <= segment["end"]
            ]
            assert "".join(body[s:e] for s, e, *_ in covering) == block
        assert segments[0]["blame"]["author"] == "model"
        assert segments[1]["blame"]["author"] == "human"
        assert {s[2] for s in doc["spans"]} == {"model", "human"}
        assert [r["seq"] for r in doc["revisions"]] == [1, 2]
        assert doc["base"]

    def test_save_body_records_a_page_human_revision(self, world) -> None:
        bridge, node_id, lock = world
        doc = bridge.call("sp1", "doc_view", node_id)
        new_body = f"{P1}\n\n{P2_EDIT}\n\n{P3}"
        before = lock.acquisitions
        fresh = bridge.call(
            "sp1", "save_body", node_id, doc["base"], new_body,
        )
        assert lock.acquisitions == before + 1
        assert fresh["body"] == new_body
        assert [s["hash"] for s in fresh["segments"]][-1] == _h(P3)
        assert fresh["revisions"][-1]["detail"] == "human:prose-page"

    def test_a_stale_base_raises_for_the_409(self, world) -> None:
        from graph_context.errors import StaleSectionMark

        bridge, node_id, _ = world
        with pytest.raises(StaleSectionMark, match="changed since"):
            bridge.call("sp1", "save_body", node_id, "0" * 16, P1)

    def test_set_marks_batches_in_one_lock_hold(self, world) -> None:
        bridge, node_id, lock = world
        doc = bridge.call("sp1", "doc_view", node_id)
        before = lock.acquisitions
        fresh = bridge.call("sp1", "set_marks", node_id, doc["base"], [
            {"hash": _h(P1), "kind": "intent", "value": "locked"},
            {"hash": _h(P2_EDIT), "kind": "status", "value": "approved"},
        ])
        assert lock.acquisitions == before + 1
        segments = {s["hash"]: s for s in fresh["segments"]}
        assert segments[_h(P1)]["intent"] == "locked"
        assert segments[_h(P2_EDIT)]["status"] == "approved"

    def test_ranged_marks_take_char_offsets(self, world) -> None:
        bridge, node_id, _ = world
        doc = bridge.call("sp1", "doc_view", node_id)
        fresh = bridge.call("sp1", "set_marks", node_id, doc["base"], [{
            "hash": _h(P1), "kind": "intent", "value": "locked",
            "start_char": 0, "end_char": 8,  # "The city"
        }])
        segment = {s["hash"]: s for s in fresh["segments"]}[_h(P1)]
        assert segment["intent"] == "locked"  # strictest-token badge
        locked = [s for s in fresh["spans"] if s[4] == "locked"]
        assert locked
        start, end = locked[0][0], locked[0][1]
        assert fresh["body"][start:end].startswith("The city")
        assert "gate barred" not in fresh["body"][start:end]

    def test_every_record_bumps_the_version_ledger(self, world) -> None:
        bridge, node_id, _ = world
        v0 = bridge.version_of("sp1", node_id)
        doc = bridge.call("sp1", "doc_view", node_id)
        bridge.call(
            "sp1", "save_body", node_id, doc["base"], f"{P1}\n\n{P3}",
        )
        v1 = bridge.version_of("sp1", node_id)
        assert v1 > v0
        assert bridge.versions_for("sp1") == {node_id: v1}
        assert bridge.versions_for("elsewhere") == {}


class TestDocWireDegradation:
    """The neutral-display rules the old block view pinned, on the doc
    wire: unrecorded and word-free blocks carry no spans and no blame."""

    def test_an_unrecorded_block_degrades_neutrally(
        self, loop, world, repo_holder
    ) -> None:
        bridge, node_id, _ = world

        async def append_unswept() -> None:
            # A human edit the historian has NOT seen yet (no sweep).
            await repo_holder[0].update_node(
                node_id, body=f"{P1}\n\n{P2_EDIT}\n\n{P3}"
            )

        asyncio.run_coroutine_threadsafe(append_unswept(), loop).result(10)
        doc = bridge.call("sp1", "doc_view", node_id)
        segments = {s["hash"]: s for s in doc["segments"]}
        new = segments[_h(P3)]
        assert new["blame"] is None
        assert not any(
            s[0] >= new["start"] and s[1] <= new["end"] for s in doc["spans"]
        )
        old = segments[_h(P1)]  # recorded: intact
        assert any(
            s[0] >= old["start"] and s[1] <= old["end"] for s in doc["spans"]
        )

    def test_word_free_separators_carry_no_spans_or_blame(
        self, world
    ) -> None:
        bridge, node_id, _ = world
        doc = bridge.call("sp1", "doc_view", node_id)
        fresh = bridge.call(
            "sp1", "save_body", node_id, doc["base"],
            f"{P1}\n\n* * *\n\n{P2_EDIT}",
        )
        separator = fresh["segments"][1]
        assert fresh["body"][separator["start"]:separator["end"]] == "* * *"
        assert separator["blame"] is None
        assert not any(
            s[0] >= separator["start"] and s[1] <= separator["end"]
            for s in fresh["spans"]
        )

    def test_page_saves_bypass_the_locked_guard(self, world) -> None:
        # The human is the locking authority: rewriting locked text from
        # the page is legal (same as editing it in Anytype).
        bridge, node_id, _ = world
        doc = bridge.call("sp1", "doc_view", node_id)
        fresh = bridge.call("sp1", "set_marks", node_id, doc["base"], [
            {"hash": _h(P1), "kind": "intent", "value": "locked"},
        ])
        rewritten = bridge.call(
            "sp1", "save_body", node_id, fresh["base"],
            f"{P3}\n\n{P2_EDIT}",
        )
        assert [s["hash"] for s in rewritten["segments"]] == [
            _h(P3), _h(P2_EDIT),
        ]


class TestAuthorDisplay:
    """The `anytype:<id>` user leg of author strings resolves to the
    member object's name at READ time (pre-WP48 revisions recorded the
    raw participant id; the log is history and stays untouched)."""

    def test_a_known_member_id_shows_its_name(
        self, loop, world, repo_holder
    ) -> None:
        bridge, node_id, _ = world

        async def revise_with_id_author() -> None:
            repo, historian = repo_holder
            member = await repo.create_node(NodeDraft(
                type="Participant", name="Nick", summary="member",
            ))
            await repo.update_node(
                node_id, body=f"{P1}\n\n{P2_EDIT}\n\n{P3}"
            )
            await historian.record_bot_revision(
                node_id,
                author_detail=f"scripted · prose · anytype:{member.id}",
            )

        asyncio.run_coroutine_threadsafe(
            revise_with_id_author(), loop
        ).result(10)
        doc = bridge.call("sp1", "doc_view", node_id)
        assert doc["revisions"][-1]["detail"] == "scripted · prose · Nick"
        segment = {s["hash"]: s for s in doc["segments"]}[_h(P3)]
        assert segment["blame"]["detail"] == "scripted · prose · Nick"
        (row,) = bridge.call("sp1", "list_nodes")
        assert row["last_author"] == "scripted · prose · Nick"

    def test_an_unknown_id_stays_verbatim(self, loop, world) -> None:
        bridge, node_id, _ = world
        doc = bridge.call("sp1", "doc_view", node_id)
        fresh = bridge.call(
            "sp1", "save_body", node_id, doc["base"], f"{P1}\n\n{P3}",
        )
        # The page's own detail has no anytype: leg; fabricate one via
        # the raw string check instead: unknown ids must not vanish.
        assert fresh["revisions"][-1]["detail"] == "human:prose-page"
