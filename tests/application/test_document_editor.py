"""DocumentEditor (WP42): hash-anchored single-section edits compose
through the session's NodeWriter, so the locked guard, staleness rule,
and journal all apply without new enforcement points."""

from __future__ import annotations

import pytest

from graph_context.application.document_editor import DocumentEditor
from graph_context.application.node_historian import NodeHistorian
from graph_context.application.node_writer import NodeWriter
from graph_context.domain import revisions
from graph_context.domain.models import NodeDraft
from graph_context.domain.session import SessionState
from graph_context.errors import LockedSectionsChanged, SectionAnchorNotFound
from graph_context.infrastructure.memory.fake_repository import (
    InMemoryGraphRepository,
)

P1 = "The city fell quiet before the siege began, every gate barred."
P2 = "Mira counted the engines twice; one was missing from the yard."
P3 = "Rain came at dusk and the watch fires guttered along the wall."


def _h(text: str) -> str:
    return revisions.block_hash(revisions.normalize_block(text))


class World:
    def __init__(self) -> None:
        self.repo = InMemoryGraphRepository()
        self.session = SessionState()
        self.writer = NodeWriter(self.repo, self.session)
        self.editor = DocumentEditor(self.repo, self.writer)

    async def chapter(self, *paragraphs: str):
        return await self.repo.create_node(NodeDraft(
            type="Chapter", name="Chapter One", summary="ch",
            body="\n\n".join(paragraphs),
        ))


class TestEditing:
    async def test_replace_touches_exactly_one_section(self) -> None:
        world = World()
        node = await world.chapter(P1, P2)
        await world.editor.edit(
            node.id, action="replace", anchor=_h(P2), text=P3,
        )
        body = await world.repo.fetch_body(node.id)
        assert P1 in body and P3 in body and P2 not in body

    async def test_sections_lists_current_anchors(self) -> None:
        world = World()
        node = await world.chapter(P1, P2)
        assert [h for h, _ in await world.editor.sections(node.id)] == [
            _h(P1), _h(P2),
        ]

    async def test_anchor_miss_is_a_prompt_listing_real_anchors(self) -> None:
        world = World()
        node = await world.chapter(P1)
        with pytest.raises(SectionAnchorNotFound) as exc:
            await world.editor.edit(
                node.id, action="delete", anchor="feedbeefcafe",
            )
        assert _h(P1) in str(exc.value)

    async def test_edit_without_summary_flags_stale(self) -> None:
        world = World()
        node = await world.chapter(P1, P2)
        outcome = await world.editor.edit(
            node.id, action="delete", anchor=_h(P2),
        )
        assert outcome.node.summary_stale is True
        fresh = await world.editor.edit(
            node.id, action="insert_after", anchor="top", text=P3,
            summary="Now opens with the rain.",
        )
        assert fresh.node.summary_stale is False


class TestLockedComposition:
    async def _guarded_world(self) -> tuple[World, str]:
        world = World()
        await world.repo.create_node(NodeDraft(
            type="gc_space_context", name="Space Context", summary="cfg",
            fields={revisions.FIELD_TRACKED_TYPES: "Chapter"},
        ))
        node = await world.chapter(P1, P2)
        historian = NodeHistorian(world.repo)
        await historian.record_bot_revision(node.id, author_detail="m")
        await historian.record_mark(
            node.id, kind="intent", block_hash=_h(P1),
            value="locked", by="user",
        )
        world.writer = NodeWriter(
            world.repo, world.session, section_guard=historian,
        )
        world.editor = DocumentEditor(world.repo, world.writer)
        return world, node.id

    async def test_editing_a_locked_section_is_refused(self) -> None:
        world, node_id = await self._guarded_world()
        with pytest.raises(LockedSectionsChanged):
            await world.editor.edit(
                node_id, action="replace", anchor=_h(P1), text=P3,
            )
        assert P1 in await world.repo.fetch_body(node_id)

    async def test_editing_another_section_passes_by_construction(self) -> None:
        world, node_id = await self._guarded_world()
        await world.editor.edit(
            node_id, action="replace", anchor=_h(P2), text=P3,
        )
        body = await world.repo.fetch_body(node_id)
        assert P1 in body and P3 in body
