"""WP6/WP12 acceptance: the binding IS the boundary; specs are data.

Binding tests assert on the DEFINITION (a non-mutating spec's table
literally lacks the mutation tools). Pipeline tests drive a scripted fake
LLM through modes against the in-memory backend -- including a script
that TRIES to mutate in a read-only mode. Loader tests pin the ADR 015
config story: profile defaults, TOML overlay, loud failures.
"""

from __future__ import annotations

import pytest

from graph_context.application.intent_recorder import IntentRecorder
from graph_context.application.mutation_journal import MutationJournal
from graph_context.domain import attribution
from graph_context.domain.schema import Role
from graph_context.domain.session import SessionState
from graph_context.errors import GraphContextError
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.infrastructure.memory.fake_session_store import InMemorySessionStore
from graph_context.interface.profiles import (
    DEFAULT_ACTIVITY_DETAIL,
    TOOL_NAMES,
    CapturePolicy,
    ModeSpec,
    get_profile,
)
from graph_context.interface.services import Services, build_services
from graph_context.orchestrator import modes
from graph_context.orchestrator.driver_common import assembled_system_prompt
from graph_context.orchestrator.drivers import (
    LLMTurn,
    ScriptedDriver,
    ToolCall,
    TranscriptEvent,
)
from graph_context.orchestrator.modes import (
    MUTATION_TOOLS,
    ModeRegistry,
    binding_for,
    load_registry,
)
from graph_context.orchestrator.pipeline import (
    LAST_TURN_WARNING,
    ConversationMemory,
    Orchestrator,
    sender_attributed,
)

FICTION = get_profile("fiction")
AUTHORING = next(s for s in FICTION.mode_specs if s.name == "authoring")
WORLD_MODELING = next(s for s in FICTION.mode_specs if s.name == "world_modeling")


class TestBindings:
    def test_mutating_spec_binds_the_full_surface(self) -> None:
        assert set(binding_for(WORLD_MODELING)) == set(TOOL_NAMES)

    def test_read_only_spec_literally_lacks_mutation_tools(self) -> None:
        """The acceptance criterion, asserted on the definition."""
        bound = set(binding_for(AUTHORING))
        assert bound.isdisjoint(MUTATION_TOOLS)
        assert bound == set(TOOL_NAMES) - MUTATION_TOOLS

    def test_tool_docs_follow_the_binding(self) -> None:
        docs = modes.tool_docs(AUTHORING, FICTION)
        assert set(docs) == set(binding_for(AUTHORING))
        assert all(docs.values())  # docstrings are prompts; never empty


class TestRegistryLoader:
    def test_profile_defaults_load_with_profile_default_mode(self) -> None:
        registry = load_registry(FICTION)
        assert registry.names() == ["authoring", "world_modeling"]
        assert registry.default == "world_modeling"

    def test_modes_file_adds_and_overrides(self, tmp_path) -> None:
        modes_file = tmp_path / "modes.toml"
        modes_file.write_text('''
[modes.record_procedure]
goal = "Notate each step the user takes so it can be repeated later."

[modes.record_procedure.capture]
artifact_type = "procedure"
min_chars = 120

[modes.authoring]
goal = "Overridden authoring goal."
''')
        registry = load_registry(FICTION, str(modes_file))
        procedure = registry.get("record_procedure")
        assert procedure is not None and procedure.capture is not None
        assert procedure.capture.artifact_type == "procedure"
        assert procedure.capture.min_chars == 120
        assert not procedure.mutating  # the safe default
        authoring = registry.get("authoring")
        assert authoring is not None
        assert authoring.goal == "Overridden authoring goal."
        assert authoring.capture is None  # override REPLACES the spec

    def test_bad_specs_fail_loudly_at_load(self, tmp_path) -> None:
        missing_goal = tmp_path / "bad1.toml"
        missing_goal.write_text("[modes.broken]\nmutating = true\n")
        with pytest.raises(GraphContextError, match="goal"):
            load_registry(FICTION, str(missing_goal))

        unknown_key = tmp_path / "bad2.toml"
        unknown_key.write_text('[modes.broken]\ngoal = "g"\nprompt = "typo"\n')
        with pytest.raises(GraphContextError, match="unknown keys"):
            load_registry(FICTION, str(unknown_key))

        with pytest.raises(GraphContextError, match="not found"):
            load_registry(FICTION, str(tmp_path / "absent.toml"))


def _mode_payload(**overrides) -> dict:
    """One in-space Activity Mode payload, ModeStore-port shaped."""
    payload = {
        "name": "Faithful Scribe",
        "goal": "Record only what the user explicitly states.",
        "mutating": True,
        "capture": None,
        "origin": "'Faithful Scribe' (obj-1)",
    }
    payload.update(overrides)
    return payload


class TestInSpaceOverlay:
    """ADR 015 amendment: the space's Activity Mode objects win."""

    def test_in_space_adds_a_mode_with_a_slugged_name(self) -> None:
        registry = load_registry(FICTION, in_space=[_mode_payload()])
        scribe = registry.get("faithful_scribe")
        assert scribe is not None and scribe.mutating
        assert scribe.goal == "Record only what the user explicitly states."
        assert registry.default == "world_modeling"  # untouched

    def test_in_space_overrides_profile_and_toml(self, tmp_path) -> None:
        modes_file = tmp_path / "modes.toml"
        modes_file.write_text('[modes.authoring]\ngoal = "From the TOML."\n')
        registry = load_registry(
            FICTION, str(modes_file),
            in_space=[_mode_payload(name="Authoring", goal="From the space.",
                                    mutating=False)],
        )
        authoring = registry.get("authoring")
        assert authoring is not None
        assert authoring.goal == "From the space."  # in-space wins

    def test_in_space_capture_fills_policy_defaults(self) -> None:
        registry = load_registry(FICTION, in_space=[_mode_payload(
            capture={"artifact_type": "note", "min_chars": 120.0},
        )])
        spec = registry.get("faithful_scribe")
        assert spec is not None and spec.capture is not None
        assert spec.capture.artifact_type == "note"
        assert spec.capture.min_chars == 120  # coerced to int
        assert spec.capture.references_label == "references"  # the default

    def test_empty_goal_names_the_object_and_the_fix(self) -> None:
        with pytest.raises(GraphContextError, match="page body"):
            load_registry(FICTION, in_space=[_mode_payload(goal="  ")])

    def test_unusable_name_fails_loudly(self) -> None:
        with pytest.raises(GraphContextError, match="letters and digits"):
            load_registry(FICTION, in_space=[_mode_payload(name="!!!")])

    def test_duplicate_slugs_name_both_objects(self) -> None:
        first = _mode_payload(origin="'Faithful Scribe' (obj-1)")
        second = _mode_payload(name="faithful   SCRIBE",
                               origin="'faithful   SCRIBE' (obj-2)")
        with pytest.raises(GraphContextError) as excinfo:
            load_registry(FICTION, in_space=[first, second])
        assert "obj-1" in str(excinfo.value) and "obj-2" in str(excinfo.value)

    def test_bad_min_chars_is_rejected(self) -> None:
        with pytest.raises(GraphContextError, match="min_chars"):
            load_registry(FICTION, in_space=[_mode_payload(
                capture={"artifact_type": "note", "min_chars": -3},
            )])


@pytest.fixture
def services() -> Services:
    return build_services(
        InMemoryGraphRepository(role_overrides=FICTION.role_overrides),
        SessionState(project="Ashfall"),
    )


def _orchestrator(services: Services, turns: list[LLMTurn]) -> Orchestrator:
    return Orchestrator(
        services=services, driver=ScriptedDriver(turns), profile=FICTION,
        registry=load_registry(FICTION),
    )


CREATE_MIRA = ToolCall("create_node", {
    "type": "Character", "name": "Mira", "summary": "Exiled siege engineer.",
})


class _TranscriptRecordingDriver(ScriptedDriver):
    """Scripted, but keeps what the pipeline SHOWED it at each decision."""

    def __init__(self, turns: list[LLMTurn]) -> None:
        super().__init__(turns)
        self.transcripts: list[tuple[TranscriptEvent, ...]] = []

    async def decide(
        self, transcript, tools, goal: str = "", *,
        web_search: bool = False, model: str = "",
    ) -> LLMTurn:
        self.transcripts.append(tuple(transcript))
        return await super().decide(transcript, tools, goal)


class TestConversationMemoryBounds:
    def test_event_cap_drops_the_oldest_turn(self) -> None:
        memory = ConversationMemory(max_events=4)
        for i in range(3):
            memory.remember_turn(f"q{i}", f"a{i}")
        texts = [e.text for e in memory.events()]
        assert texts == ["q1", "a1", "q2", "a2"]

    def test_char_cap_evicts_oldest_first(self) -> None:
        memory = ConversationMemory(max_chars=20)
        memory.remember_turn("x" * 15, "y" * 15)
        memory.remember_turn("new q", "new a")
        assert [e.text for e in memory.events()] == ["new q", "new a"]

    def test_seed_replaces_and_applies_the_same_bounds(self) -> None:
        memory = ConversationMemory(max_events=2)
        memory.remember_turn("old", "old")
        memory.seed([("user", "a"), ("assistant", "b"), ("user", "c")])
        assert [e.text for e in memory.events()] == ["b", "c"]


class TestPipeline:
    async def test_mutating_mode_turn_creates_and_replies(
        self, services: Services
    ) -> None:
        orchestrator = _orchestrator(services, [
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="Mira now exists."),
        ])
        events = await orchestrator.handle_message("s1", "u1", "Add Mira.")
        assert [e.kind for e in events] == ["reply"]
        assert services.repository.graph.find_by_name("Mira")  # it really ran

    async def test_read_only_mode_cannot_mutate(self, services: Services) -> None:
        orchestrator = _orchestrator(services, [
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="I tried."),
        ])
        await orchestrator.handle_message("s1", "u1", "/mode authoring")
        events = await orchestrator.handle_message("s1", "u1", "Add Mira.")
        errors = [e for e in events if e.kind == "error"]
        assert errors and "not available in authoring mode" in errors[0].text
        assert not services.repository.graph.find_by_name("Mira")  # nothing ran

    async def test_modes_are_per_session_with_registry_default(
        self, services: Services
    ) -> None:
        orchestrator = _orchestrator(services, [
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="done"),
        ])
        await orchestrator.handle_message("locked-down", "u1", "/mode authoring")
        assert orchestrator.mode_of("locked-down") == "authoring"
        assert orchestrator.mode_of("fresh") == "world_modeling"
        events = await orchestrator.handle_message("fresh", "u1", "Add Mira.")
        assert events[-1].kind == "reply"

    async def test_mode_command_lists_loaded_specs(self, services: Services) -> None:
        orchestrator = _orchestrator(services, [])
        current = await orchestrator.handle_message("s1", "u1", "/mode")
        assert current[0].kind == "notice"
        assert "world_modeling" in current[0].text and "authoring" in current[0].text
        bad = await orchestrator.handle_message("s1", "u1", "/mode chaos")
        assert bad[0].kind == "error" and "authoring" in bad[0].text

    async def test_turn_opens_with_the_context_block_exactly_once(
        self, services: Services
    ) -> None:
        """WP15: the block is the transcript's first event and is assembled
        once per turn -- later decisions in the same turn see the same
        single block, never a second copy."""
        services.session.scratchpad = "open thread: the gate"
        driver = _TranscriptRecordingDriver([
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="done"),
        ])
        orchestrator = Orchestrator(
            services=services, driver=driver, profile=FICTION,
            registry=load_registry(FICTION),
        )
        await orchestrator.handle_message("s1", "u1", "Add Mira.")
        assert len(driver.transcripts) == 2  # two decisions in the turn
        for transcript in driver.transcripts:
            blocks = [
                e for e in transcript if e.text.startswith("[session context")
            ]
            assert len(blocks) == 1
            assert transcript[0] is blocks[0]
        assert "open thread: the gate" in driver.transcripts[0][0].text

    async def test_empty_session_injects_no_block(
        self, services: Services
    ) -> None:
        driver = _TranscriptRecordingDriver([LLMTurn(reply="hi")])
        orchestrator = Orchestrator(
            services=services, driver=driver, profile=FICTION,
            registry=load_registry(FICTION),
        )
        await orchestrator.handle_message("s1", "u1", "hello")
        (transcript,) = driver.transcripts
        assert [e.text for e in transcript] == ["hello"]

    async def test_conversation_memory_replays_previous_turns(
        self, services: Services
    ) -> None:
        driver = _TranscriptRecordingDriver([
            LLMTurn(reply="Hi there."), LLMTurn(reply="Again."),
        ])
        orchestrator = Orchestrator(
            services=services, driver=driver, profile=FICTION,
            registry=load_registry(FICTION),
        )
        await orchestrator.handle_message("s1", "u1", "hello")
        await orchestrator.handle_message("s1", "u1", "and again")
        second = [(e.kind, e.text) for e in driver.transcripts[1]]
        assert second[0] == ("user", "hello")
        assert second[1] == ("assistant", "Hi there.")
        assert second[-1] == ("user", "and again")

    async def test_sender_attribution_reaches_the_model_and_memory(
        self, services: Services
    ) -> None:
        """A session can be a shared chat, so each message must say who
        sent it (live-caught: Task Creation Mode could not fill
        'Assignee = the requester' from a bare message)."""
        driver = _TranscriptRecordingDriver([
            LLMTurn(reply="Hi Nick."), LLMTurn(reply="Hi Sam."),
        ])
        orchestrator = Orchestrator(
            services=services, driver=driver, profile=FICTION,
            registry=load_registry(FICTION),
        )
        await orchestrator.handle_message("s1", "u1", "hello", sender="Nick")
        assert driver.transcripts[0][-1].text == "[from Nick] hello"
        await orchestrator.handle_message("s1", "u2", "me too", sender="Sam")
        replayed = [(e.kind, e.text) for e in driver.transcripts[1]]
        assert replayed[0] == ("user", "[from Nick] hello")
        assert replayed[-1] == ("user", "[from Sam] me too")

    def test_the_sender_tag_matches_its_system_prompt_description(self) -> None:
        """The drivers' standing guidance tells the model the [from <name>]
        tag is authoritative (live-caught: a model burned its whole tool
        budget searching the graph for the sender instead); the tag format
        and its description must stay in lockstep."""
        assert sender_attributed("hello", "Nick") == "[from Nick] hello"
        assert '"[from <name>]"' in assembled_system_prompt("any goal")

    async def test_memory_is_per_session(self, services: Services) -> None:
        driver = _TranscriptRecordingDriver([
            LLMTurn(reply="a"), LLMTurn(reply="b"),
        ])
        orchestrator = Orchestrator(
            services=services, driver=driver, profile=FICTION,
            registry=load_registry(FICTION),
        )
        await orchestrator.handle_message("chat-one", "u1", "first chat")
        await orchestrator.handle_message("chat-two", "u1", "second chat")
        assert "first chat" not in [e.text for e in driver.transcripts[1]]

    async def test_clear_empties_memory_and_keeps_session_state(
        self, services: Services
    ) -> None:
        services.session.scratchpad = "kept across /clear"
        driver = _TranscriptRecordingDriver([
            LLMTurn(reply="remembered"), LLMTurn(reply="fresh"),
        ])
        orchestrator = Orchestrator(
            services=services, driver=driver, profile=FICTION,
            registry=load_registry(FICTION),
        )
        await orchestrator.handle_message("s1", "u1", "before the clear")
        cleared = await orchestrator.handle_message("s1", "u1", "/clear")
        assert cleared[0].kind == "notice"
        assert "memory cleared" in cleared[0].text
        await orchestrator.handle_message("s1", "u1", "after the clear")
        last = [e.text for e in driver.transcripts[-1]]
        assert not any("before the clear" in t for t in last)
        assert any("kept across /clear" in t for t in last)  # block survives

    async def test_seed_memory_primes_a_session(self, services: Services) -> None:
        driver = _TranscriptRecordingDriver([LLMTurn(reply="ok")])
        orchestrator = Orchestrator(
            services=services, driver=driver, profile=FICTION,
            registry=load_registry(FICTION),
        )
        await orchestrator.seed_memory(
            "s1", [("user", "earlier question"), ("assistant", "earlier answer")]
        )
        await orchestrator.handle_message("s1", "u1", "follow-up")
        (transcript,) = driver.transcripts
        assert [(e.kind, e.text) for e in transcript][:2] == [
            ("user", "earlier question"), ("assistant", "earlier answer"),
        ]

    async def test_driver_receives_the_active_goal(self, services: Services) -> None:
        """ADR 015: the spec's goal prompt reaches the driver each step."""
        goals: list[str] = []

        class GoalSpy:
            async def decide(self, transcript, tools, goal, *, web_search=False, model=""):  # type: ignore[no-untyped-def]
                goals.append(goal)
                return LLMTurn(reply="ok")

        orchestrator = Orchestrator(
            services=services, driver=GoalSpy(), profile=FICTION,
            registry=load_registry(FICTION),
        )
        await orchestrator.handle_message("s1", "u1", "hello")
        await orchestrator.handle_message("s1", "u1", "/mode authoring")
        await orchestrator.handle_message("s1", "u1", "write")
        assert goals[0] == WORLD_MODELING.goal
        assert goals[1] == AUTHORING.goal

    async def test_mode_command_refreshes_the_registry(
        self, services: Services
    ) -> None:
        """ADR 015 amendment: edit the Activity Mode object in Anytype,
        send /mode, and the new spec is live -- no restart."""
        payloads: list[dict] = []

        async def reload():
            return load_registry(FICTION, in_space=payloads)

        orchestrator = Orchestrator(
            services=services, driver=ScriptedDriver([]), profile=FICTION,
            registry=load_registry(FICTION), reload_registry=reload,
        )
        payloads.append(_mode_payload())  # the human creates the object
        events = await orchestrator.handle_message("s1", "u1", "/mode")
        assert "faithful_scribe" in events[-1].text
        switched = await orchestrator.handle_message(
            "s1", "u1", "/mode faithful_scribe"
        )
        assert switched[-1].kind == "notice"
        assert orchestrator.mode_of("s1") == "faithful_scribe"

    async def test_failed_refresh_keeps_the_last_good_registry(
        self, services: Services
    ) -> None:
        async def reload():
            raise GraphContextError("Activity Mode 'Broken' (obj-9): the "
                                    "goal is empty")

        orchestrator = Orchestrator(
            services=services, driver=ScriptedDriver([]), profile=FICTION,
            registry=load_registry(FICTION), reload_registry=reload,
        )
        events = await orchestrator.handle_message("s1", "u1", "/mode authoring")
        errors = [e for e in events if e.kind == "error"]
        assert errors and "obj-9" in errors[0].text  # actionable, names it
        # the switch still worked against the previously loaded registry
        assert orchestrator.mode_of("s1") == "authoring"

    async def test_vanished_mode_falls_back_to_the_default(
        self, services: Services
    ) -> None:
        payloads = [_mode_payload()]

        async def reload():
            return load_registry(FICTION, in_space=payloads)

        orchestrator = Orchestrator(
            services=services, driver=ScriptedDriver([LLMTurn(reply="ok")]),
            profile=FICTION, registry=load_registry(FICTION),
            reload_registry=reload,
        )
        await orchestrator.handle_message("s1", "u1", "/mode faithful_scribe")
        payloads.clear()  # the human archives the object
        events = await orchestrator.handle_message("s1", "u1", "/mode")
        assert any(
            e.kind == "notice" and "no longer loaded" in e.text for e in events
        )
        assert orchestrator.mode_of("s1") == "world_modeling"

    async def test_vanished_mode_mid_turn_degrades_without_dying(
        self, services: Services
    ) -> None:
        """A refresh from one session may drop another session's mode; the
        next turn in that session must degrade to the default, not crash."""
        payloads = [_mode_payload()]

        async def reload():
            return load_registry(FICTION, in_space=payloads)

        orchestrator = Orchestrator(
            services=services, driver=ScriptedDriver([LLMTurn(reply="ok")]),
            profile=FICTION, registry=load_registry(FICTION),
            reload_registry=reload,
        )
        await orchestrator.handle_message("a", "u1", "/mode faithful_scribe")
        payloads.clear()
        await orchestrator.handle_message("b", "u2", "/mode")  # b refreshes
        events = await orchestrator.handle_message("a", "u1", "hello")
        assert events[-1].kind == "reply"
        assert orchestrator.mode_of("a") == "world_modeling"

    async def test_tool_budget_cuts_a_runaway_turn(self, services: Services) -> None:
        probe = ToolCall("context", {"action": "get"})
        orchestrator = Orchestrator(
            services=services,
            driver=ScriptedDriver([LLMTurn(tool_calls=(probe,))] * 99),
            profile=FICTION, registry=load_registry(FICTION),
            max_tool_calls=3,
        )
        events = await orchestrator.handle_message("s1", "u1", "loop forever")
        assert events[-1].kind == "notice"
        assert "budget exhausted" in events[-1].text

    async def test_only_the_final_decision_is_warned(
        self, services: Services
    ) -> None:
        """The driver hears about the cutoff exactly once, right before its
        last decision, so it can answer instead of being cut off."""
        probe = ToolCall("context", {"action": "get"})
        driver = _TranscriptRecordingDriver([
            LLMTurn(tool_calls=(probe,)),
            LLMTurn(tool_calls=(probe,)),
            LLMTurn(reply="Best answer from what I gathered."),
        ])
        orchestrator = Orchestrator(
            services=services, driver=driver, profile=FICTION,
            registry=load_registry(FICTION), max_tool_calls=3,
        )
        events = await orchestrator.handle_message("s1", "u1", "dig deep")
        warned = [
            any(e.text == LAST_TURN_WARNING for e in transcript)
            for transcript in driver.transcripts
        ]
        assert warned == [False, False, True]
        # the warned driver replied, so the turn ends normally: no notice
        assert [e.kind for e in events] == ["reply"]
        assert events[0].text == "Best answer from what I gathered."

    async def test_final_decision_bundles_a_last_update_with_the_reply(
        self, services: Services
    ) -> None:
        """A warned driver may land one last update AND answer: the calls
        run, and the text that is normally ignored preamble IS the reply."""
        orchestrator = _orchestrator(services, [
            LLMTurn(reply="Mira now exists.", tool_calls=(CREATE_MIRA,)),
        ])
        orchestrator.max_tool_calls = 1
        events = await orchestrator.handle_message("s1", "u1", "Add Mira.")
        assert services.repository.graph.find_by_name("Mira")  # update ran
        assert [e.kind for e in events] == ["reply"]  # and no cutoff notice
        assert events[0].text == "Mira now exists."

    async def test_final_update_without_reply_text_is_still_cut_short(
        self, services: Services
    ) -> None:
        orchestrator = _orchestrator(services, [LLMTurn(tool_calls=(CREATE_MIRA,))])
        orchestrator.max_tool_calls = 1
        events = await orchestrator.handle_message("s1", "u1", "Add Mira.")
        assert services.repository.graph.find_by_name("Mira")  # update ran
        assert events[-1].kind == "notice"
        assert "budget exhausted" in events[-1].text

    async def test_preamble_text_on_a_non_final_decision_is_not_a_reply(
        self, services: Services
    ) -> None:
        orchestrator = _orchestrator(services, [
            LLMTurn(reply="Creating Mira now...", tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="Mira now exists."),
        ])
        events = await orchestrator.handle_message("s1", "u1", "Add Mira.")
        assert [e.text for e in events] == ["Mira now exists."]


def _provenance_orchestrator(
    turns: list[LLMTurn],
    *,
    extra_specs: tuple[ModeSpec, ...] = (),
) -> tuple[Orchestrator, Services]:
    journal = MutationJournal()
    services = build_services(
        InMemoryGraphRepository(role_overrides=FICTION.role_overrides),
        SessionState(project="Ashfall"),
        journal=journal,
    )
    registry = load_registry(FICTION)
    if extra_specs:
        registry = modes.ModeRegistry(
            specs={**registry.specs, **{s.name: s for s in extra_specs}},
            default=registry.default,
        )
    orchestrator = Orchestrator(
        services=services,
        driver=ScriptedDriver(turns),
        profile=FICTION,
        registry=registry,
        provenance=IntentRecorder(services.repository, now=lambda: "T0"),
        model_name="scripted",
    )
    return orchestrator, services


def _intent_nodes(services: Services) -> list:
    return [n for n in services.repository.graph.nodes() if n.role is Role.INTENT]


def _keyed_orchestrator(
    turns: list[LLMTurn],
    *,
    store: InMemorySessionStore | None = None,
    driver=None,
):
    """An orchestrator with a real per-session-key Services factory (WP8):
    one shared repository, a keyed session store, independent SessionState
    per session id -- the multi-chat shape."""
    from graph_context.application.session_registry import SessionRegistry
    from graph_context.interface.services import derive_services

    store = store or InMemorySessionStore()
    repository = InMemoryGraphRepository(role_overrides=FICTION.role_overrides)
    base = build_services(repository, SessionState(project="Ashfall"))
    registry = SessionRegistry(store)

    async def services_for(key: str) -> Services:
        session, persister = await registry.get(key)
        return derive_services(base, session, persister)

    orchestrator = Orchestrator(
        services=base, driver=driver or ScriptedDriver(turns), profile=FICTION,
        registry=load_registry(FICTION), services_for=services_for,
    )
    return orchestrator, store


class TestKeyedSessions:
    """WP8: each session id gets its own SessionState + persisted mode."""

    async def test_two_chats_have_independent_working_sets(self) -> None:
        note_a = ToolCall("context", {"action": "note", "text": "arc: the siege"})
        note_b = ToolCall("context", {"action": "note", "text": "arc: the exile"})
        orchestrator, _ = _keyed_orchestrator([])
        orchestrator.driver = ScriptedDriver([  # per-turn scripts
            LLMTurn(tool_calls=(note_a,)), LLMTurn(reply="a noted"),
            LLMTurn(tool_calls=(note_b,)), LLMTurn(reply="b noted"),
        ])
        await orchestrator.handle_message("anytype:a", "u1", "note the siege")
        await orchestrator.handle_message("anytype:b", "u1", "note the exile")
        services_a = orchestrator.services_of("anytype:a")
        services_b = orchestrator.services_of("anytype:b")
        assert services_a is not None and services_b is not None
        assert services_a.session.scratchpad == "arc: the siege"
        assert services_b.session.scratchpad == "arc: the exile"
        assert services_a.session is not services_b.session

    async def test_mode_switch_persists_per_chat_and_survives_restart(self) -> None:
        store = InMemorySessionStore()
        orchestrator, _ = _keyed_orchestrator([], store=store)
        await orchestrator.handle_message("anytype:a", "u1", "/mode authoring")
        await orchestrator.handle_message("anytype:b", "u1", "hi")  # stays default
        # A fresh orchestrator over the same store == a restart.
        restarted, _ = _keyed_orchestrator([LLMTurn(reply="ok")], store=store)
        assert restarted.mode_of("anytype:a") == "world_modeling"  # not yet seen
        await restarted.handle_message("anytype:a", "u1", "resume")
        assert restarted.mode_of("anytype:a") == "authoring"  # restored on first turn
        await restarted.handle_message("anytype:b", "u1", "resume")
        assert restarted.mode_of("anytype:b") == "world_modeling"

    async def test_persisted_but_vanished_mode_degrades_to_default(self) -> None:
        store = InMemorySessionStore()
        # Seed a snapshot naming a mode this profile does not load.
        seed = SessionState(mode="ghost_mode")
        await store.save(seed.to_snapshot(), "anytype:a")
        orchestrator, _ = _keyed_orchestrator([LLMTurn(reply="ok")], store=store)
        await orchestrator.handle_message("anytype:a", "u1", "hi")
        assert orchestrator.mode_of("anytype:a") == "world_modeling"

    async def test_mode_switch_flush_failure_degrades_to_a_notice(self) -> None:
        class Flaky(InMemorySessionStore):
            async def save(self, snapshot, key):
                raise GraphContextError("store on fire")

        orchestrator, _ = _keyed_orchestrator([], store=Flaky())
        events = await orchestrator.handle_message("anytype:a", "u1", "/mode authoring")
        # The switch still happened in memory; a notice explains it won't persist.
        assert orchestrator.mode_of("anytype:a") == "authoring"
        assert any("could not be saved" in e.text for e in events)


class _RecordingObserver:
    """A TurnObserver that just remembers what it was told (WP19)."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    async def turn_started(self, mode: str, detail: str) -> None:
        self.events.append(("turn_started", mode, detail))

    async def decision(self, turn: LLMTurn) -> None:
        self.events.append(
            ("decision", tuple(c.name for c in turn.tool_calls))
        )

    async def tool_result(self, call: ToolCall, result: str, ok: bool) -> None:
        self.events.append(("tool_result", call.name, ok))


class TestTurnObserver:
    """WP19 (ADR 029): the per-turn event tap for live activity surfaces."""

    async def test_observer_sees_the_whole_turn(
        self, services: Services
    ) -> None:
        orchestrator = _orchestrator(services, [
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="Mira now exists."),
        ])
        observer = _RecordingObserver()
        await orchestrator.handle_message(
            "s1", "u1", "create Mira", observer=observer
        )
        assert observer.events == [
            ("turn_started", "world_modeling", DEFAULT_ACTIVITY_DETAIL),
            ("decision", ("create_node",)),
            ("tool_result", "create_node", True),
            ("decision", ()),
        ]

    async def test_a_failing_tool_reports_ok_false(
        self, services: Services
    ) -> None:
        bad = ToolCall("context", {"action": "bogus"})
        orchestrator = _orchestrator(services, [
            LLMTurn(tool_calls=(bad,)), LLMTurn(reply="oops"),
        ])
        observer = _RecordingObserver()
        await orchestrator.handle_message(
            "s1", "u1", "make it", observer=observer
        )
        assert ("tool_result", "context", False) in observer.events

    async def test_an_unavailable_tool_reports_ok_false(
        self, services: Services
    ) -> None:
        orchestrator = _orchestrator(services, [
            LLMTurn(tool_calls=(CREATE_MIRA,)), LLMTurn(reply="denied"),
        ])
        observer = _RecordingObserver()
        await orchestrator.handle_message("s1", "u1", "/mode authoring")
        await orchestrator.handle_message(
            "s1", "u1", "create Mira", observer=observer
        )
        assert ("tool_result", "create_node", False) in observer.events

    async def test_command_turns_never_stream(
        self, services: Services
    ) -> None:
        orchestrator = _orchestrator(services, [])
        observer = _RecordingObserver()
        await orchestrator.handle_message(
            "s1", "u1", "/mode authoring", observer=observer
        )
        await orchestrator.handle_message(
            "s1", "u1", "/clear", observer=observer
        )
        assert observer.events == []


class TestActivityDetailFromTheMode:
    """WP19 (ADR 029 amendment): the detail level is a MODE property --
    picking a mode picks its live-activity verbosity."""

    async def test_bare_mode_reports_the_active_modes_detail(
        self, services: Services
    ) -> None:
        orchestrator = _orchestrator(services, [])
        events = await orchestrator.handle_message("s1", "u1", "/mode")
        assert (
            f"(activity detail: {DEFAULT_ACTIVITY_DETAIL}; web search: off; "
            "model: default)"
            in events[-1].text
        )

    async def test_switching_modes_switches_the_streamed_detail(
        self, services: Services
    ) -> None:
        chatty = ModeSpec(
            name="narrator", goal="Narrate.", activity_detail="full"
        )
        registry = load_registry(FICTION)
        specs = dict(registry.specs) | {chatty.name: chatty}
        orchestrator = Orchestrator(
            services=services,
            driver=ScriptedDriver([LLMTurn(reply="ok"), LLMTurn(reply="ok")]),
            profile=FICTION,
            registry=ModeRegistry(specs=specs, default=registry.default),
        )
        observer = _RecordingObserver()
        await orchestrator.handle_message("s1", "u1", "hi", observer=observer)
        assert observer.events[0] == (
            "turn_started", "world_modeling", DEFAULT_ACTIVITY_DETAIL,
        )
        await orchestrator.handle_message("s1", "u1", "/mode narrator")
        observer.events.clear()
        await orchestrator.handle_message("s1", "u1", "hi", observer=observer)
        assert observer.events[0] == ("turn_started", "narrator", "full")

    def test_a_modes_file_can_set_the_detail(self, tmp_path) -> None:
        path = tmp_path / "modes.toml"
        path.write_text(
            '[modes.quiet]\ngoal = "Work silently."\n'
            'activity_detail = "off"\n'
        )
        registry = load_registry(FICTION, modes_file=str(path))
        spec = registry.get("quiet")
        assert spec is not None and spec.activity_detail == "off"

    def test_an_in_space_mode_object_can_set_the_detail(self) -> None:
        registry = load_registry(
            FICTION,
            in_space=[_mode_payload(activity_detail="Tools ")],  # UI-typed
        )
        spec = registry.get("faithful_scribe")
        assert spec is not None and spec.activity_detail == "tools"

    def test_an_unset_detail_takes_the_default(self) -> None:
        registry = load_registry(FICTION, in_space=[_mode_payload()])
        spec = registry.get("faithful_scribe")
        assert spec is not None
        assert spec.activity_detail == DEFAULT_ACTIVITY_DETAIL

    def test_an_unknown_detail_fails_loudly_naming_the_source(
        self, tmp_path
    ) -> None:
        path = tmp_path / "modes.toml"
        path.write_text(
            '[modes.loud]\ngoal = "g"\nactivity_detail = "verbose"\n'
        )
        with pytest.raises(GraphContextError) as err:
            load_registry(FICTION, modes_file=str(path))
        assert "[modes.loud]" in str(err.value)
        assert "off, minimal, tools, full" in str(err.value)


class TestWebSearchFromTheMode:
    """WP20 (ADR 030): web search is a MODE property, default off --
    picking a mode picks whether the provider's server-side search tool
    is admitted; the pipeline forwards the flag on every decide."""

    def test_a_modes_file_can_enable_web_search(self, tmp_path) -> None:
        path = tmp_path / "modes.toml"
        path.write_text(
            '[modes.researcher]\ngoal = "Look things up."\nweb_search = true\n'
        )
        registry = load_registry(FICTION, modes_file=str(path))
        spec = registry.get("researcher")
        assert spec is not None and spec.web_search is True

    def test_an_in_space_mode_object_can_enable_web_search(self) -> None:
        registry = load_registry(
            FICTION, in_space=[_mode_payload(web_search=True)]
        )
        spec = registry.get("faithful_scribe")
        assert spec is not None and spec.web_search is True

    def test_web_search_defaults_off(self) -> None:
        registry = load_registry(FICTION, in_space=[_mode_payload()])
        spec = registry.get("faithful_scribe")
        assert spec is not None and spec.web_search is False

    async def test_the_pipeline_forwards_the_active_modes_flag(
        self, services: Services
    ) -> None:
        forwarded: list[bool] = []

        class FlagSpy:
            def system_prompt(self, goal: str) -> str:
                return goal

            def render_prompt(self, transcript) -> str:  # type: ignore[no-untyped-def]
                return ""

            async def decide(self, transcript, tools, goal, *, web_search=False, model=""):  # type: ignore[no-untyped-def]
                forwarded.append(web_search)
                return LLMTurn(reply="ok")

        searching = ModeSpec(
            name="researcher", goal="Look things up.", web_search=True
        )
        registry = load_registry(FICTION)
        specs = dict(registry.specs) | {searching.name: searching}
        orchestrator = Orchestrator(
            services=services,
            driver=FlagSpy(),
            profile=FICTION,
            registry=ModeRegistry(specs=specs, default=registry.default),
        )
        await orchestrator.handle_message("s1", "u1", "hi")
        await orchestrator.handle_message("s1", "u1", "/mode researcher")
        await orchestrator.handle_message("s1", "u1", "hi again")
        assert forwarded == [False, True]

    async def test_bare_mode_reports_web_search_on(
        self, services: Services
    ) -> None:
        searching = ModeSpec(
            name="researcher", goal="Look things up.", web_search=True
        )
        registry = load_registry(FICTION)
        specs = dict(registry.specs) | {searching.name: searching}
        orchestrator = Orchestrator(
            services=services,
            driver=ScriptedDriver([]),
            profile=FICTION,
            registry=ModeRegistry(specs=specs, default=registry.default),
        )
        await orchestrator.handle_message("s1", "u1", "/mode researcher")
        events = await orchestrator.handle_message("s1", "u1", "/mode")
        assert "web search: on" in events[-1].text


class TestModelFromTheMode:
    """ADR 033: the Claude model is a MODE property, default unset --
    picking a mode picks which model runs its decisions; the pipeline
    resolves the choice to a provider model id on every decide."""

    def test_a_modes_file_can_pin_the_model(self, tmp_path) -> None:
        path = tmp_path / "modes.toml"
        path.write_text(
            '[modes.heavy]\ngoal = "Think hard."\nmodel = "opus 4.8"\n'
        )
        registry = load_registry(FICTION, modes_file=str(path))
        spec = registry.get("heavy")
        assert spec is not None and spec.model == "opus 4.8"

    def test_an_in_space_mode_object_can_pin_the_model(self) -> None:
        registry = load_registry(
            FICTION, in_space=[_mode_payload(model="Sonnet 5 ")],  # UI-typed
        )
        spec = registry.get("faithful_scribe")
        assert spec is not None and spec.model == "sonnet 5"

    def test_the_model_defaults_unset(self) -> None:
        registry = load_registry(FICTION, in_space=[_mode_payload()])
        spec = registry.get("faithful_scribe")
        assert spec is not None and spec.model == ""

    def test_an_unknown_model_fails_loudly_naming_the_source(
        self, tmp_path
    ) -> None:
        path = tmp_path / "modes.toml"
        path.write_text('[modes.heavy]\ngoal = "g"\nmodel = "haiku 3"\n')
        with pytest.raises(GraphContextError) as err:
            load_registry(FICTION, modes_file=str(path))
        assert "[modes.heavy]" in str(err.value)
        assert "sonnet 5, opus 4.8, fable 5" in str(err.value)

    async def test_the_pipeline_forwards_the_resolved_model_id(
        self, services: Services
    ) -> None:
        forwarded: list[str] = []

        class ModelSpy:
            def system_prompt(self, goal: str) -> str:
                return goal

            def render_prompt(self, transcript) -> str:  # type: ignore[no-untyped-def]
                return ""

            async def decide(self, transcript, tools, goal, *, web_search=False, model=""):  # type: ignore[no-untyped-def]
                forwarded.append(model)
                return LLMTurn(reply="ok")

        pinned = ModeSpec(
            name="heavy", goal="Think hard.", model="opus 4.8"
        )
        registry = load_registry(FICTION)
        specs = dict(registry.specs) | {pinned.name: pinned}
        orchestrator = Orchestrator(
            services=services,
            driver=ModelSpy(),
            profile=FICTION,
            registry=ModeRegistry(specs=specs, default=registry.default),
        )
        await orchestrator.handle_message("s1", "u1", "hi")
        await orchestrator.handle_message("s1", "u1", "/mode heavy")
        await orchestrator.handle_message("s1", "u1", "hi again")
        assert forwarded == ["", "claude-opus-4-8"]

    async def test_bare_mode_reports_the_pinned_model(
        self, services: Services
    ) -> None:
        pinned = ModeSpec(name="heavy", goal="Think hard.", model="fable 5")
        registry = load_registry(FICTION)
        specs = dict(registry.specs) | {pinned.name: pinned}
        orchestrator = Orchestrator(
            services=services,
            driver=ScriptedDriver([]),
            profile=FICTION,
            registry=ModeRegistry(specs=specs, default=registry.default),
        )
        await orchestrator.handle_message("s1", "u1", "/mode heavy")
        events = await orchestrator.handle_message("s1", "u1", "/mode")
        assert "model: fable 5" in events[-1].text


class TestServerToolContinuity:
    """WP22 (ADR 030 amendment): a decision's provider-executed searches
    ride the recorded decision event -- the NEXT decide sees the calls
    and their raw result payloads, turn-locally."""

    async def test_the_next_decide_sees_the_searches(
        self, services: Services
    ) -> None:
        raw = '{"content": [{"title": "A", "url": "https://a"}]}'
        searching = LLMTurn(
            tool_calls=(ToolCall("find_node", {"name": "Mira"}),),
            server_tool_calls=(
                ToolCall("web_search", {"query": "mira"}, id="s1"),
            ),
            server_tool_results=(raw,),
        )
        driver = _TranscriptRecordingDriver(
            [searching, LLMTurn(reply="done")]
        )
        orchestrator = Orchestrator(
            services=services, driver=driver, profile=FICTION,
            registry=load_registry(FICTION),
        )
        await orchestrator.handle_message("s1", "u1", "look this up")
        second = driver.transcripts[1]
        decision = next(e for e in second if e.kind == "assistant")
        assert decision.server_tool_calls == searching.server_tool_calls
        assert decision.server_tool_results == (raw,)


class TestDefaultModeOverride:
    """WP21: spaces.toml can name the mode NEW chats start in. Sessions
    with a persisted mode keep it (the pipeline only consults
    ``registry.default`` when no mode was persisted)."""

    def test_a_given_default_wins_over_the_profiles(self) -> None:
        registry = load_registry(FICTION, in_space=[_mode_payload()])
        assert registry.default == "world_modeling"  # the profile's
        overridden = load_registry(
            FICTION, in_space=[_mode_payload()], default="faithful_scribe"
        )
        assert overridden.default == "faithful_scribe"

    def test_an_unknown_default_fails_loudly_listing_loaded_modes(self) -> None:
        with pytest.raises(GraphContextError) as err:
            load_registry(FICTION, default="nope")
        assert "default_mode 'nope'" in str(err.value)
        assert "world_modeling" in str(err.value)


class TestProvenanceTurns:
    """WP7 end-to-end at the seam: one intent node per mutating turn."""

    async def test_mutating_turn_records_one_intent_with_trace(self) -> None:
        orchestrator, services = _provenance_orchestrator([
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="Mira exists."),
        ])
        await orchestrator.handle_message("s1", "cli:nick", "Add Mira.")
        (intent,) = _intent_nodes(services)
        assert intent.name.startswith("Intent: Add Mira.")
        assert intent.fields[attribution.FIELD_USER_ID] == "cli:nick"
        assert intent.fields[attribution.FIELD_MODE] == "world_modeling"  # the active binding
        mira = services.repository.graph.resolve("Mira")
        assert {e.target for e in services.repository.graph.edges(intent.id)} == {
            mira.id
        }

    async def test_read_only_turn_records_nothing(self) -> None:
        orchestrator, services = _provenance_orchestrator([
            LLMTurn(tool_calls=(ToolCall("context", {"action": "get"}),)),
            LLMTurn(reply="All quiet."),
        ])
        await orchestrator.handle_message("s1", "u", "How big is the world?")
        assert _intent_nodes(services) == []

    async def test_capture_policy_threshold_is_respected(self) -> None:
        """A custom spec with a lower threshold captures what the default
        would ignore -- the policy, not a constant, decides."""
        eager = ModeSpec(
            name="eager_capture", goal="capture everything",
            capture=CapturePolicy(min_chars=10),
        )
        short_scene = "Mira waits in the vault dark."  # < 200, > 10; names Mira
        orchestrator, services = _provenance_orchestrator([
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="created"),
            LLMTurn(reply=short_scene),
        ], extra_specs=(eager,))
        await orchestrator.handle_message("s1", "u", "Add Mira.")
        await orchestrator.handle_message("s1", "u", "/mode eager_capture")
        await orchestrator.handle_message("s1", "u", "Write a beat.")
        graph = services.repository.graph
        prose = [n for n in graph.nodes() if n.role is Role.CAPTURE]
        assert len(prose) == 1
        mira = graph.resolve("Mira")
        assert {
            e.target for e in graph.edges(prose[0].id) if e.type == "references"
        } == {mira.id}

    async def test_default_authoring_threshold_skips_short_replies(self) -> None:
        orchestrator, services = _provenance_orchestrator([
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="created"),
            LLMTurn(reply="Mira nods."),  # mentions her, conversation-sized
        ])
        await orchestrator.handle_message("s1", "u", "Add Mira.")
        await orchestrator.handle_message("s1", "u", "/mode authoring")
        await orchestrator.handle_message("s1", "u", "Does she agree?")
        assert [n for n in services.repository.graph.nodes()
                if n.role is Role.CAPTURE] == []

    async def test_subsystem_off_records_nothing(self, services: Services) -> None:
        orchestrator = _orchestrator(services, [
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="done"),
        ])  # no provenance wired
        await orchestrator.handle_message("s1", "u", "Add Mira.")
        assert _intent_nodes(services) == []


class TestToolRoundTripTranscript:
    """The pipeline records each tool-call decision on the transcript,
    paired to its results by id -- what a Messages-API driver needs to
    round-trip native tool_use/tool_result blocks."""

    async def test_the_decision_precedes_its_results_with_matching_ids(
        self, services: Services
    ) -> None:
        driver = _TranscriptRecordingDriver([
            LLMTurn(reply="Checking.", tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="Done."),
        ])
        orchestrator = Orchestrator(
            services=services, driver=driver, profile=FICTION,
            registry=load_registry(FICTION),
        )
        await orchestrator.handle_message("s1", "u1", "Add Mira.")
        second = driver.transcripts[1]
        decision = second[-2]
        result = second[-1]
        assert decision.kind == "assistant"
        assert decision.text == "Checking."
        assert len(decision.tool_calls) == 1
        assert decision.tool_calls[0].name == "create_node"
        assert decision.tool_calls[0].id  # synthesized when the driver sent none
        assert result.kind == "tool"
        assert result.tool_use_id == decision.tool_calls[0].id

    async def test_driver_provided_ids_are_preserved(
        self, services: Services
    ) -> None:
        call = ToolCall(
            "create_node",
            {"type": "Character", "name": "Mira", "summary": "Engineer."},
            id="toolu_real_api_id",
        )
        driver = _TranscriptRecordingDriver([
            LLMTurn(tool_calls=(call,)),
            LLMTurn(reply="Done."),
        ])
        orchestrator = Orchestrator(
            services=services, driver=driver, profile=FICTION,
            registry=load_registry(FICTION),
        )
        await orchestrator.handle_message("s1", "u1", "Add Mira.")
        second = driver.transcripts[1]
        assert second[-2].tool_calls[0].id == "toolu_real_api_id"
        assert second[-1].tool_use_id == "toolu_real_api_id"

    async def test_mid_turn_events_never_reach_conversation_memory(
        self, services: Services
    ) -> None:
        driver = _TranscriptRecordingDriver([
            LLMTurn(tool_calls=(CREATE_MIRA,)),
            LLMTurn(reply="Mira now exists."),
            LLMTurn(reply="Second turn."),
        ])
        orchestrator = Orchestrator(
            services=services, driver=driver, profile=FICTION,
            registry=load_registry(FICTION),
        )
        await orchestrator.handle_message("s1", "u1", "Add Mira.")
        await orchestrator.handle_message("s1", "u1", "And now?")
        replayed = driver.transcripts[2]  # the second turn's history
        assert [(e.kind, e.text) for e in replayed[:2]] == [
            ("user", "Add Mira."), ("assistant", "Mira now exists."),
        ]
        # The tool-call decision and its result stayed turn-local.
        assert all(e.tool_calls == () and e.tool_use_id == "" for e in replayed)


class TestOutboundFileEvents:
    """WP23: the send_file tool's queue drains into ``file`` reply events
    after the reply, and never leaks across turns."""

    async def test_queued_files_ride_out_after_the_reply(
        self, services: Services
    ) -> None:
        orchestrator = _orchestrator(services, [
            LLMTurn(tool_calls=(
                ToolCall("send_file", {"name": "a.csv", "content": "a,b"}),
            )),
            LLMTurn(reply="here you go"),
        ])
        events = await orchestrator.handle_message("s1", "u1", "export it")
        assert [(e.kind, e.text) for e in events] == [
            ("reply", "here you go"), ("file", "a,b"),
        ]
        assert events[-1].file_name == "a.csv"

    async def test_the_outbox_is_turn_scoped(self, services: Services) -> None:
        # A file left behind (e.g. the turn crashed after queueing) must
        # not ride out with the NEXT turn's reply.
        from graph_context.interface.services import OutboundFile

        services.outbox.append(OutboundFile(name="stale.md", content="old"))
        orchestrator = _orchestrator(services, [LLMTurn(reply="fresh turn")])
        events = await orchestrator.handle_message("s1", "u1", "hi")
        assert [e.kind for e in events] == ["reply"]
