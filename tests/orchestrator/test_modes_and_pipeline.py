"""WP6 acceptance: the binding IS the boundary; the pipeline proves it.

The mode tests assert on the binding DEFINITION (authoring literally lacks
the mutation tools), not on refusal behavior. The pipeline tests drive a
scripted fake LLM through both modes against the in-memory backend --
including a script that TRIES to mutate in authoring mode.
"""

from __future__ import annotations

import pytest

from graph_context.domain.models import NodeDraft
from graph_context.domain.session import SessionState
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.interface.profiles import TOOL_NAMES, get_profile
from graph_context.interface.tools import Services, build_services
from graph_context.orchestrator import modes
from graph_context.orchestrator.drivers import LLMTurn, ScriptedDriver, ToolCall
from graph_context.orchestrator.modes import MUTATION_TOOLS, TOOL_BINDINGS, Mode
from graph_context.orchestrator.pipeline import Orchestrator


class TestBindings:
    def test_world_modeling_binds_the_full_surface(self) -> None:
        assert set(TOOL_BINDINGS[Mode.WORLD_MODELING]) == set(TOOL_NAMES)

    def test_authoring_binding_literally_lacks_mutation_tools(self) -> None:
        """The WP6 acceptance criterion, asserted on the definition."""
        bound = set(TOOL_BINDINGS[Mode.AUTHORING])
        assert bound.isdisjoint(MUTATION_TOOLS)
        assert bound == set(TOOL_NAMES) - MUTATION_TOOLS

    def test_tool_docs_follow_the_binding(self) -> None:
        profile = get_profile("fiction")
        docs = modes.tool_docs(Mode.AUTHORING, profile)
        assert set(docs) == set(TOOL_BINDINGS[Mode.AUTHORING])
        assert all(docs.values())  # docstrings are prompts; never empty


@pytest.fixture
def services() -> Services:
    profile = get_profile("fiction")
    return build_services(
        InMemoryGraphRepository(role_overrides=profile.role_overrides),
        SessionState(project="Ashfall"),
    )


def _orchestrator(services: Services, turns: list[LLMTurn]) -> Orchestrator:
    return Orchestrator(
        services=services, driver=ScriptedDriver(turns), profile=get_profile("fiction")
    )


CREATE_MIRA = ToolCall("create_node", {
    "type": "Character", "name": "Mira", "summary": "Exiled siege engineer.",
})


class TestPipeline:
    async def test_world_modeling_turn_creates_and_replies(
        self, services: Services
    ) -> None:
        orchestrator = _orchestrator(services, [
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="Mira now exists."),
        ])
        events = await orchestrator.handle_message("s1", "u1", "Add Mira.")
        assert [e.kind for e in events] == ["reply"]
        assert events[0].text == "Mira now exists."
        assert services.repository.graph.find_by_name("Mira")  # it really ran

    async def test_authoring_mode_cannot_mutate(self, services: Services) -> None:
        orchestrator = _orchestrator(services, [
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="I tried."),
        ])
        await orchestrator.handle_message("s1", "u1", "/mode authoring")
        events = await orchestrator.handle_message("s1", "u1", "Add Mira.")
        errors = [e for e in events if e.kind == "error"]
        assert errors and "not available in authoring mode" in errors[0].text
        assert "create_node" not in errors[0].text.split("available: ")[1]
        assert not services.repository.graph.find_by_name("Mira")  # nothing ran

    async def test_read_tools_still_work_in_authoring(
        self, services: Services
    ) -> None:
        await services.writer.create_node(
            NodeDraft("Character", name="Mira", summary="Engineer.")
        )
        orchestrator = _orchestrator(services, [
            LLMTurn(tool_calls=(ToolCall("get_node", {"node_id": "Mira"}),)),
            LLMTurn(reply="Found her."),
        ])
        await orchestrator.handle_message("s1", "u1", "/mode authoring")
        events = await orchestrator.handle_message("s1", "u1", "Who is Mira?")
        assert [e.kind for e in events] == ["reply"]

    async def test_modes_are_per_session(self, services: Services) -> None:
        orchestrator = _orchestrator(services, [
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="done"),
        ])
        await orchestrator.handle_message("locked-down", "u1", "/mode authoring")
        assert orchestrator.mode_of("locked-down") is Mode.AUTHORING
        assert orchestrator.mode_of("fresh") is Mode.WORLD_MODELING
        # The fresh session mutates fine; the authoring one never saw the tool.
        events = await orchestrator.handle_message("fresh", "u1", "Add Mira.")
        assert events[-1].kind == "reply"
        assert services.repository.graph.find_by_name("Mira")

    async def test_mode_command_reports_and_validates(
        self, services: Services
    ) -> None:
        orchestrator = _orchestrator(services, [])
        current = await orchestrator.handle_message("s1", "u1", "/mode")
        assert current[0].kind == "notice" and "world_modeling" in current[0].text
        bad = await orchestrator.handle_message("s1", "u1", "/mode chaos")
        assert bad[0].kind == "error"
        assert "authoring" in bad[0].text and "world_modeling" in bad[0].text
        switched = await orchestrator.handle_message("s1", "u1", "/mode authoring")
        assert switched[0].kind == "notice"
        assert "create_node" not in switched[0].text  # bound-tools listing

    async def test_tool_budget_cuts_a_runaway_turn(self, services: Services) -> None:
        probe = ToolCall("context", {"action": "get"})
        orchestrator = Orchestrator(
            services=services,
            driver=ScriptedDriver([LLMTurn(tool_calls=(probe,))] * 99),
            profile=get_profile("fiction"),
            max_tool_calls=3,
        )
        events = await orchestrator.handle_message("s1", "u1", "loop forever")
        assert events[-1].kind == "notice"
        assert "budget exhausted" in events[-1].text

    async def test_driver_error_text_lets_the_model_self_correct(
        self, services: Services
    ) -> None:
        """The unavailable-tool notice is also appended to the transcript,
        so a real driver can pick a bound tool on its next step."""
        seen: list[str] = []

        class SpyDriver:
            def __init__(self) -> None:
                self._turns = ScriptedDriver([
                    LLMTurn(tool_calls=(CREATE_MIRA,)),
                    LLMTurn(reply="ok"),
                ])

            async def decide(self, transcript, tools):  # type: ignore[no-untyped-def]
                seen.extend(e.text for e in transcript if e.kind == "tool")
                return await self._turns.decide(transcript, tools)

        orchestrator = Orchestrator(
            services=services, driver=SpyDriver(), profile=get_profile("fiction")
        )
        await orchestrator.handle_message("s1", "u1", "/mode authoring")
        await orchestrator.handle_message("s1", "u1", "Add Mira.")
        assert any("not available in authoring mode" in text for text in seen)
