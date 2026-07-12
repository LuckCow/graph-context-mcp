"""The turn-start context block (WP15): content, budget ladder, safety.

The block is LLM-facing copy assembled once per orchestrator turn; these
tests pin its shape the way the profile goldens pin docstrings.
"""

from __future__ import annotations

import pytest

from graph_context.domain.models import Detail
from graph_context.interface import tools
from graph_context.interface.context_block import build_turn_context
from graph_context.interface.services import build_services

pytestmark = pytest.mark.usefixtures("world")


class TestEmptySession:
    async def test_fresh_session_renders_nothing(self, repository) -> None:
        # A genuinely fresh session (the fixture world's writes have
        # already touched the shared one): zero tokens on a cold start.
        from graph_context.domain.session import SessionState

        fresh = build_services(repository, SessionState(project="Ashfall"))
        assert await build_turn_context(fresh) == ""

    async def test_recent_alone_is_enough_to_render(
        self, services, world
    ) -> None:
        services.session.touch(world.mira.id)
        block = await build_turn_context(services)
        assert "recent (automatic trail): Mira" in block  # most recent first
        assert "working set" not in block  # nothing held, no empty section


class TestContent:
    async def test_scratchpad_and_held_nodes_open_the_turn(
        self, services, world
    ) -> None:
        services.session.scratchpad = "open thread: the gate standoff"
        services.session.working_set.hold(world.mira.id, Detail.FULL)
        services.session.working_set.hold(world.undercroft.id)
        await services.repository.update_node(
            world.mira.id, body="Leads the survivors through the vaults."
        )
        block = await build_turn_context(services)
        assert block.startswith("[session context")
        assert "project: Ashfall" in block
        assert "open thread: the gate standoff" in block
        # Full bucket leads, body and one-hop edges attached.
        assert block.index("Mira (Character") < block.index(
            "The Undercroft (Location"
        )
        assert "[full]" in block and "[summaries]" in block
        assert "Leads the survivors through the vaults." in block
        assert "participated_in -> Siege of Brakk (Event)" in block

    async def test_summary_entries_carry_edges_too(self, services, world) -> None:
        services.session.working_set.hold(world.undercroft.id)
        block = await build_turn_context(services)
        assert "located_at <- Mira (Character)" in block

    async def test_prose_neighbors_stay_invisible(self, services, world) -> None:
        # Bookkeeping roles never surface in the block, same as explore.
        from graph_context.application.capture_recorder import CaptureRecorder

        recorder = CaptureRecorder(services.repository, now=lambda: "t")
        await recorder.record(
            text="A scene.", summary="s", references=[world.mira.id]
        )
        services.session.working_set.hold(world.mira.id)
        assert "Prose" not in await build_turn_context(services)

    async def test_vanished_node_is_skipped_not_fatal(
        self, services, world
    ) -> None:
        services.session.working_set.hold(world.mira.id)
        services.session.working_set.hold(world.ashbrand.id)
        services.repository.graph.remove_node(world.ashbrand.id)
        block = await build_turn_context(services)
        assert "Mira" in block and "Ashbrand" not in block


class TestBudgetLadder:
    async def test_bodies_are_dropped_first_with_an_explicit_note(
        self, services, world
    ) -> None:
        services.session.working_set.hold(world.mira.id, Detail.FULL)
        await services.repository.update_node(world.mira.id, body="x" * 2000)
        block = await build_turn_context(services, budget_chars=800)
        assert "x" * 100 not in block
        assert "[body omitted: over context budget" in block
        assert "edges:" in block  # full-entry edges survive the squeeze

    async def test_summary_edges_then_recent_drop_at_the_floor(
        self, services, world
    ) -> None:
        for node in (world.mira, world.siege, world.undercroft):
            services.session.working_set.hold(node.id)
            services.session.touch(node.id)
        floor = await build_turn_context(services, budget_chars=1)
        assert "recent" not in floor
        assert "edges:" not in floor  # all held at summaries; their edges go
        assert "Mira" in floor  # held lines are the floor, never dropped

    async def test_within_budget_keeps_everything(self, services, world) -> None:
        services.session.working_set.hold(world.mira.id, Detail.FULL)
        await services.repository.update_node(world.mira.id, body="Short body.")
        services.session.touch(world.siege.id)
        block = await build_turn_context(services)
        assert "Short body." in block
        assert "recent (automatic trail):" in block


class TestToolAndBlockAgree:
    async def test_note_then_block_round_trip(self, services, world) -> None:
        # The tool writes it, the block echoes it -- the cross-turn loop.
        await tools.context_tool(
            services, action="note", text="follow up on the vault door"
        )
        await tools.context_tool(
            services, action="hold", node_id="Mira", detail="full"
        )
        block = await build_turn_context(services)
        assert "follow up on the vault door" in block
        assert "Mira (Character" in block
