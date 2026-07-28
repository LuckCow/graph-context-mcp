"""NodeHistorian service (WP41, ADR 049): the recording contract.

The invariants under test: tracking is keyed off the Space Context's
tracked-types list (infra types never track); the first body-bearing
write creates the sidecar; identical bodies never record; rebuild
restores baselines without re-recording, and its catch-up compare picks
up offline edits as human revisions; the change-tick sweep records human
edits and ignores everything else.
"""

from __future__ import annotations

import pytest

from graph_context.application.node_historian import (
    HISTORY_TYPE,
    NodeHistorian,
)
from graph_context.domain import revisions
from graph_context.domain.models import Node, NodeDraft
from graph_context.domain.schema import Role
from graph_context.errors import (
    GraphContextError,
    LockedSectionsChanged,
    StaleSectionMark,
)
from graph_context.infrastructure.memory.fake_repository import (
    InMemoryGraphRepository,
)

OPENING = "The city fell quiet before the siege began, every gate barred."
MIDDLE = "Mira counted the engines twice; one was missing from the yard."
REVISED = "Rain came at dusk and the watch fires guttered along the wall."


class World:
    """One space with a tracked-types list and a helper per test need."""

    def __init__(self) -> None:
        self.repo = InMemoryGraphRepository()
        self.ticks = 0
        self.historian = NodeHistorian(self.repo, now=self._now)

    def _now(self) -> str:
        self.ticks += 1
        return f"T{self.ticks}"

    async def seed_space_context(self, tracked: str = "Chapter") -> Node:
        return await self.repo.create_node(NodeDraft(
            type="gc_space_context", name="Space Context", summary="cfg",
            fields={revisions.FIELD_TRACKED_TYPES: tracked},
        ))

    async def chapter(self, *paragraphs: str, name: str = "Chapter One") -> Node:
        return await self.repo.create_node(NodeDraft(
            type="Chapter", name=name, summary="a chapter",
            body="\n\n".join(paragraphs),
        ))

    def sidecars(self) -> list[Node]:
        return [
            n for n in self.repo.graph.nodes()
            if n.role is Role.NODE_HISTORY
        ]


class TestTracking:
    async def test_tracked_types_read_off_the_space_context(self) -> None:
        world = World()
        assert world.historian.tracked_types() == ()
        await world.seed_space_context("Chapter, Character")
        assert world.historian.tracked_types() == ("Chapter", "Character")

    async def test_only_listed_types_track(self) -> None:
        world = World()
        await world.seed_space_context("Chapter")
        chapter = await world.chapter(OPENING)
        other = await world.repo.create_node(NodeDraft(
            type="Character", name="Mira", summary="engineer", body=OPENING,
        ))
        assert world.historian.is_tracked(chapter.id)
        assert not world.historian.is_tracked(other.id)

    async def test_infra_types_never_track_even_if_listed(self) -> None:
        world = World()
        await world.seed_space_context("Activity Mode")
        mode = await world.repo.create_node(NodeDraft(
            type="gc_activity_mode", name="Prose", summary="m", body="goal",
        ))
        assert not world.historian.is_tracked(mode.id)

    async def test_a_list_edit_applies_with_no_restart(self) -> None:
        world = World()
        context = await world.seed_space_context("Chapter")
        character = await world.repo.create_node(NodeDraft(
            type="Character", name="Mira", summary="engineer", body=OPENING,
        ))
        assert not world.historian.is_tracked(character.id)
        await world.repo.update_node(
            context.id,
            fields={revisions.FIELD_TRACKED_TYPES: "Chapter, Character"},
        )
        assert world.historian.is_tracked(character.id)


class TestRecording:
    async def test_first_bot_revision_creates_the_sidecar(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING, MIDDLE)
        recorded = await world.historian.record_bot_revision(
            chapter.id, author_detail="fable · prose · u1",
        )
        assert recorded
        (sidecar,) = world.sidecars()
        assert sidecar.type_key == HISTORY_TYPE or sidecar.type == HISTORY_TYPE
        assert sidecar.fields[revisions.FIELD_HISTORY_OF] == chapter.id
        parsed = revisions.parse_log(await world.repo.fetch_body(sidecar.id))
        (record,) = parsed.records
        assert record.kind == revisions.KIND_KEYFRAME
        assert record.author_detail == "fable · prose · u1"

    async def test_identical_body_records_nothing(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        assert await world.historian.record_bot_revision(
            chapter.id, author_detail="m",
        )
        assert not await world.historian.record_bot_revision(
            chapter.id, author_detail="m",
        )
        assert len(world.historian.history(chapter.id)) == 1

    async def test_the_sweep_records_a_human_revision(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        await world.repo.update_node(
            chapter.id, body=f"{OPENING}\n\n{REVISED}",
        )
        await world.historian.sweep({chapter.id})
        records = world.historian.history(chapter.id)
        assert [r.author_kind for r in records] == [
            revisions.AUTHOR_MODEL, revisions.AUTHOR_HUMAN,
        ]

    async def test_a_human_created_chapter_tracks_on_first_sweep(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        await world.historian.sweep({chapter.id})
        (record,) = world.historian.history(chapter.id)
        assert record.author_kind == revisions.AUTHOR_HUMAN

    async def test_an_empty_body_does_not_start_tracking(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(name="Stub")  # no paragraphs
        await world.historian.sweep({chapter.id})
        assert world.sidecars() == []

    async def test_untracked_changes_are_ignored(self) -> None:
        world = World()
        await world.seed_space_context()
        other = await world.repo.create_node(NodeDraft(
            type="Character", name="Mira", summary="engineer", body=OPENING,
        ))
        await world.historian.sweep({other.id})
        assert world.sidecars() == []

    async def test_blame_reads_from_the_live_baseline(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        await world.repo.update_node(
            chapter.id, body=f"{OPENING}\n\n{MIDDLE}",
        )
        await world.historian.sweep({chapter.id})
        by_hash = world.historian.blame(chapter.id)
        opening = revisions.block_hash(revisions.normalize_block(OPENING))
        middle = revisions.block_hash(revisions.normalize_block(MIDDLE))
        assert by_hash[opening].author_kind == revisions.AUTHOR_MODEL
        assert by_hash[middle].author_kind == revisions.AUTHOR_HUMAN


class TestRebuild:
    async def test_rebuild_restores_without_re_recording(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        fresh = NodeHistorian(world.repo, now=lambda: "T9")
        await fresh.rebuild()
        assert fresh.tracked_ids() == frozenset({chapter.id})
        assert len(fresh.history(chapter.id)) == 1  # clean replay: no echo

    async def test_rebuild_catches_up_an_offline_edit_as_human(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        await world.repo.update_node(chapter.id, body=REVISED)  # bot was down
        fresh = NodeHistorian(world.repo, now=lambda: "T9")
        await fresh.rebuild()
        records = fresh.history(chapter.id)
        assert [r.author_kind for r in records] == [
            revisions.AUTHOR_MODEL, revisions.AUTHOR_HUMAN,
        ]

    async def test_a_mangled_sidecar_degrades_never_bricks(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        (sidecar,) = world.sidecars()
        await world.repo.update_node(sidecar.id, body="a human wiped this")
        fresh = NodeHistorian(world.repo, now=lambda: "T9")
        await fresh.rebuild()
        # The log restarts from the current body -- seq 1, keyframe.
        (record,) = fresh.history(chapter.id)
        assert record.kind == revisions.KIND_KEYFRAME
        assert record.author_kind == revisions.AUTHOR_HUMAN


def _h(text: str) -> str:
    return revisions.block_hash(revisions.normalize_block(text))


async def _tracked_chapter() -> tuple[World, Node]:
    world = World()
    await world.seed_space_context()
    chapter = await world.chapter(OPENING, MIDDLE)
    await world.historian.record_bot_revision(chapter.id, author_detail="m")
    return world, chapter


class TestSectionMarks:
    async def test_a_mark_lands_in_the_sidecar_and_survives_rebuild(self) -> None:
        world, chapter = await _tracked_chapter()
        state = await world.historian.record_mark(
            chapter.id, kind="status", block_hash=_h(OPENING),
            value="approved", by="user",
        )
        assert state.status == "approved"
        fresh = NodeHistorian(world.repo, now=lambda: "T9")
        await fresh.rebuild()
        assert fresh.section_states(chapter.id)[_h(OPENING)].status == "approved"

    async def test_re_marking_the_same_value_writes_nothing(self) -> None:
        world, chapter = await _tracked_chapter()
        await world.historian.record_mark(
            chapter.id, kind="intent", block_hash=_h(OPENING),
            value="locked", by="user",
        )
        (sidecar,) = world.sidecars()
        before = await world.repo.fetch_body(sidecar.id)
        again = await world.historian.record_mark(
            chapter.id, kind="intent", block_hash=_h(OPENING),
            value="locked", by="user",
        )
        assert again.intent == "locked"
        assert await world.repo.fetch_body(sidecar.id) == before

    async def test_a_stale_hash_is_rejected_with_reload(self) -> None:
        world, chapter = await _tracked_chapter()
        with pytest.raises(StaleSectionMark, match="reload"):
            await world.historian.record_mark(
                chapter.id, kind="status", block_hash=_h(REVISED),
                value="approved", by="user",
            )

    async def test_bad_kinds_and_values_are_prompt_errors(self) -> None:
        world, chapter = await _tracked_chapter()
        with pytest.raises(GraphContextError, match="allowed"):
            await world.historian.record_mark(
                chapter.id, kind="mood", block_hash=_h(OPENING),
                value="approved", by="user",
            )
        with pytest.raises(GraphContextError, match="allowed"):
            await world.historian.record_mark(
                chapter.id, kind="status", block_hash=_h(OPENING),
                value="golden", by="user",
            )

    async def test_short_separator_blocks_cannot_carry_marks(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING, "* * *", MIDDLE)
        await world.historian.record_bot_revision(chapter.id, author_detail="m")
        with pytest.raises(GraphContextError, match="too short"):
            await world.historian.record_mark(
                chapter.id, kind="intent", block_hash=_h("* * *"),
                value="locked", by="user",
            )

    async def test_a_never_recorded_node_is_a_prompt_error(self) -> None:
        world = World()
        await world.seed_space_context()
        chapter = await world.chapter(OPENING)
        with pytest.raises(GraphContextError, match="no revision history"):
            await world.historian.record_mark(
                chapter.id, kind="status", block_hash=_h(OPENING),
                value="approved", by="user",
            )


class TestSectionGuard:
    async def test_dropping_a_locked_section_raises_with_excerpt(self) -> None:
        world, chapter = await _tracked_chapter()
        await world.historian.record_mark(
            chapter.id, kind="intent", block_hash=_h(OPENING),
            value="locked", by="user",
        )
        with pytest.raises(LockedSectionsChanged) as exc:
            world.historian.check_body_update(chapter.id, MIDDLE)
        assert "city fell quiet" in str(exc.value)
        assert "unlock" in str(exc.value)

    async def test_a_moved_locked_section_passes(self) -> None:
        world, chapter = await _tracked_chapter()
        await world.historian.record_mark(
            chapter.id, kind="intent", block_hash=_h(OPENING),
            value="locked", by="user",
        )
        world.historian.check_body_update(
            chapter.id, "\n\n".join([MIDDLE, REVISED, OPENING])
        )

    async def test_untracked_nodes_pass_freely(self) -> None:
        world = World()
        world.historian.check_body_update("no-such-node", "anything")
