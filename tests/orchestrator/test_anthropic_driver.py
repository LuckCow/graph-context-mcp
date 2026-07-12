"""AnthropicDriver translation seams.

The API round-trip is covered by the gated live test
(``tests/e2e/test_live_anthropic_driver.py``, spends API credits); here
the pure translation logic is pinned. Self-skips where the ``anthropic``
SDK is not installed (CI installs only ``[dev]``; the ``[anthropic]``
extra rides the devcontainer image).

Response fixtures are plain namespaces exposing exactly the attributes
the driver reads (``content`` blocks, ``stop_reason``, ``usage``); the
live E2E pins that those attributes match the real SDK objects.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("anthropic")

from graph_context.errors import GraphContextError  # noqa: E402
from graph_context.orchestrator import modes  # noqa: E402
from graph_context.orchestrator.anthropic_driver import (  # noqa: E402
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    AnthropicDriver,
    anthropic_tools,
    messages_from_transcript,
    turn_from_response,
    usage_from_response,
)
from graph_context.orchestrator.driver_common import (  # noqa: E402
    assembled_system_prompt,
    derive_schema,
)
from graph_context.orchestrator.drivers import (  # noqa: E402
    ToolCall,
    TranscriptEvent,
)


def _text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(id: str, name: str, input: dict) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def _response(
    content: list[SimpleNamespace],
    stop_reason: str = "end_turn",
    usage: SimpleNamespace | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=usage
        or SimpleNamespace(
            input_tokens=10,
            output_tokens=20,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


class _StubClient:
    """Captures the request kwargs; answers with a canned response."""

    def __init__(self, response: SimpleNamespace) -> None:
        self._response = response
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self._response


class TestTranscriptMapping:
    def test_user_and_assistant_events_map_to_their_roles(self):
        messages = messages_from_transcript([
            TranscriptEvent("user", "Who is Mira?"),
            TranscriptEvent("assistant", "An exiled engineer."),
            TranscriptEvent("user", "Where does she live?"),
        ])
        assert [m["role"] for m in messages] == ["user", "assistant", "user"]
        assert messages[0]["content"] == "Who is Mira?"
        assert messages[1]["content"] == "An exiled engineer."

    def test_tool_calls_round_trip_as_native_blocks_with_matching_ids(self):
        call = ToolCall("get_node", {"node_id": "n1"}, id="toolu_1")
        messages = messages_from_transcript([
            TranscriptEvent("user", "Who is Mira?"),
            TranscriptEvent("assistant", "Looking her up.", tool_calls=(call,)),
            TranscriptEvent(
                "tool", "Mira: exiled engineer.",
                tool_name="get_node", tool_use_id="toolu_1",
            ),
        ])
        assistant = messages[1]
        assert assistant["role"] == "assistant"
        assert assistant["content"][0] == {"type": "text", "text": "Looking her up."}
        assert assistant["content"][1] == {
            "type": "tool_use", "id": "toolu_1", "name": "get_node",
            "input": {"node_id": "n1"},
        }
        result = messages[2]
        assert result["role"] == "user"
        assert result["content"] == [{
            "type": "tool_result", "tool_use_id": "toolu_1",
            "content": "Mira: exiled engineer.",
        }]

    def test_a_text_empty_tool_call_decision_emits_no_text_block(self):
        call = ToolCall("get_node", {"node_id": "n1"}, id="toolu_1")
        messages = messages_from_transcript([
            TranscriptEvent("user", "Who is Mira?"),
            TranscriptEvent("assistant", "", tool_calls=(call,)),
        ])
        assert messages[1]["content"] == [{
            "type": "tool_use", "id": "toolu_1", "name": "get_node",
            "input": {"node_id": "n1"},
        }]

    def test_consecutive_tool_results_merge_into_one_user_message(self):
        calls = (
            ToolCall("get_node", {"node_id": "n1"}, id="toolu_1"),
            ToolCall("get_node", {"node_id": "n2"}, id="toolu_2"),
        )
        messages = messages_from_transcript([
            TranscriptEvent("user", "Compare Mira and Joss."),
            TranscriptEvent("assistant", "", tool_calls=calls),
            TranscriptEvent("tool", "Mira.", tool_name="get_node",
                            tool_use_id="toolu_1"),
            TranscriptEvent("tool", "Joss.", tool_name="get_node",
                            tool_use_id="toolu_2"),
        ])
        assert len(messages) == 3
        results = messages[2]["content"]
        assert [r["tool_use_id"] for r in results] == ["toolu_1", "toolu_2"]

    def test_a_leading_assistant_event_gets_a_synthetic_user_opener(self):
        # Memory eviction is event-granular: a replayed history can open
        # with an orphaned reply half. messages[0] must be a user turn.
        messages = messages_from_transcript([
            TranscriptEvent("assistant", "An exiled engineer."),
            TranscriptEvent("user", "Where does she live?"),
        ])
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        assert messages[2]["role"] == "user"

    def test_an_orphan_tool_result_falls_back_to_fenced_text(self):
        # No prior tool_use block carries this id (or any id at all), so a
        # tool_result block would 400 -- degrade to the fenced-text shape.
        messages = messages_from_transcript([
            TranscriptEvent("user", "Who is Mira?"),
            TranscriptEvent("tool", "Mira.", tool_name="get_node",
                            tool_use_id="toolu_missing"),
        ])
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == (
            '<tool_result tool="get_node">\nMira.\n</tool_result>'
        )

    def test_paired_and_orphan_results_in_one_run_both_survive(self):
        call = ToolCall("get_node", {"node_id": "n1"}, id="toolu_1")
        messages = messages_from_transcript([
            TranscriptEvent("user", "Compare."),
            TranscriptEvent("assistant", "", tool_calls=(call,)),
            TranscriptEvent("tool", "Mira.", tool_name="get_node",
                            tool_use_id="toolu_1"),
            TranscriptEvent("tool", "Joss.", tool_name="get_node",
                            tool_use_id=""),
        ])
        paired, orphan = messages[2], messages[3]
        assert paired["content"][0]["tool_use_id"] == "toolu_1"
        assert "<tool_result" in orphan["content"]


class TestToolDefinitions:
    def test_tools_are_sorted_strict_and_closed(self):
        schemas = {"get_node": derive_schema(modes.full_surface()["get_node"])}
        tools = anthropic_tools(
            {"get_node": "Fetch one node.", "explore": "Walk the graph."}, schemas
        )
        assert [t["name"] for t in tools] == ["explore", "get_node"]
        get_node = tools[1]
        assert get_node["description"] == "Fetch one node."
        assert get_node["strict"] is True
        assert get_node["input_schema"]["additionalProperties"] is False
        assert "node_id" in get_node["input_schema"]["properties"]

    def test_a_name_without_a_schema_degrades_to_bare_object_without_strict(self):
        tools = anthropic_tools({"explore": "Walk the graph."}, {})
        assert tools[0]["input_schema"] == {"type": "object"}
        # strict requires additionalProperties:false + required, which a
        # bare object lacks -- the API would reject the pairing.
        assert "strict" not in tools[0]

    def test_every_surface_tool_builds_a_definition(self):
        surface = modes.full_surface()
        schemas = {name: derive_schema(fn) for name, fn in surface.items()}
        docs = {name: (fn.__doc__ or name) for name, fn in surface.items()}
        tools = anthropic_tools(docs, schemas)
        assert len(tools) == len(surface)
        for definition in tools:
            assert definition["strict"] is True


class TestResponseHarvest:
    def test_text_only_becomes_the_reply(self):
        turn = turn_from_response(_response([_text_block("She lives in Vel.")]))
        assert turn.reply == "She lives in Vel."
        assert turn.tool_calls == ()

    def test_tool_use_blocks_become_calls_with_ids_preserved(self):
        turn = turn_from_response(_response([
            _text_block("Let me check both."),
            _tool_use_block("toolu_a", "get_node", {"node_id": "n1"}),
            _tool_use_block("toolu_b", "explore", {"start": "n1"}),
        ], stop_reason="tool_use"))
        assert turn.reply == "Let me check both."
        assert [c.id for c in turn.tool_calls] == ["toolu_a", "toolu_b"]
        assert turn.tool_calls[0].name == "get_node"
        assert turn.tool_calls[0].arguments == {"node_id": "n1"}

    def test_thinking_blocks_are_skipped(self):
        thinking = SimpleNamespace(type="thinking", thinking="")
        turn = turn_from_response(_response([thinking, _text_block("Answer.")]))
        assert turn.reply == "Answer."

    def test_a_refusal_yields_a_notice_and_no_calls(self):
        turn = turn_from_response(_response([], stop_reason="refusal"))
        assert turn.reply
        assert "declined" in turn.reply
        assert turn.tool_calls == ()

    def test_a_max_tokens_cut_is_annotated_as_truncation(self):
        turn = turn_from_response(
            _response([_text_block("Partial ans")], stop_reason="max_tokens")
        )
        assert turn.reply.startswith("Partial ans")
        assert "truncated" in turn.reply


class TestUsageTranslation:
    def test_the_usage_block_maps_field_for_field(self):
        response = _response([], usage=SimpleNamespace(
            input_tokens=11, output_tokens=220,
            cache_read_input_tokens=3000, cache_creation_input_tokens=450,
        ))
        usage = usage_from_response(response, duration_ms=4200)
        assert usage.duration_ms == 4200
        assert usage.input_tokens == 11
        assert usage.output_tokens == 220
        assert usage.cache_read_tokens == 3000
        assert usage.cache_creation_tokens == 450
        assert usage.num_turns == 1

    def test_the_api_reports_tokens_not_dollars(self):
        usage = usage_from_response(_response([]), duration_ms=1)
        assert usage.total_cost_usd is None

    def test_missing_cache_fields_degrade_to_zeroes_never_raise(self):
        response = _response([], usage=SimpleNamespace(
            input_tokens=1, output_tokens=2,
        ))
        usage = usage_from_response(response, duration_ms=1)
        assert usage.cache_read_tokens == 0
        assert usage.cache_creation_tokens == 0


class TestRequestShape:
    """What actually goes over the wire, pinned via the injectable client."""

    @pytest.fixture
    def stub(self):
        return _StubClient(_response([_text_block("Hi.")]))

    async def _decide(self, stub, **driver_kwargs):
        driver = AnthropicDriver(schemas={}, client=stub, **driver_kwargs)
        await driver.decide(
            [TranscriptEvent("user", "Hello")], {"get_node": "Fetch."}, "Be terse."
        )
        return stub.requests[0]

    async def test_the_request_carries_the_expected_parameters(self, stub):
        request = await self._decide(stub)
        assert request["model"] == DEFAULT_MODEL
        assert request["max_tokens"] == DEFAULT_MAX_TOKENS
        assert request["system"] == assembled_system_prompt("Be terse.")
        assert request["thinking"] == {"type": "adaptive"}
        assert request["messages"] == [{"role": "user", "content": "Hello"}]
        assert [t["name"] for t in request["tools"]] == ["get_node"]

    async def test_no_sampling_parameters_are_sent(self, stub):
        request = await self._decide(stub)
        assert "temperature" not in request
        assert "top_p" not in request
        assert "top_k" not in request

    async def test_effort_is_sent_only_when_configured(self, stub):
        request = await self._decide(stub, effort="low")
        assert request["output_config"] == {"effort": "low"}
        stub.requests.clear()
        request = await self._decide(stub)
        assert "output_config" not in request

    async def test_the_usage_observer_fires_once_per_decide(self, stub):
        seen = []
        driver = AnthropicDriver(schemas={}, client=stub, on_result=seen.append)
        await driver.decide([TranscriptEvent("user", "Hello")], {}, "")
        assert len(seen) == 1
        assert seen[0].input_tokens == 10

    async def test_api_errors_surface_as_graph_context_errors(self):
        class _FailingClient:
            def __init__(self):
                self.messages = SimpleNamespace(create=self._create)

            async def _create(self, **kwargs):
                import anthropic
                import httpx

                raise anthropic.APIConnectionError(
                    request=httpx.Request("POST", "https://api.anthropic.com")
                )

        driver = AnthropicDriver(schemas={}, client=_FailingClient())
        with pytest.raises(GraphContextError, match="api.anthropic.com"):
            await driver.decide([TranscriptEvent("user", "Hi")], {}, "")


class TestSeamParity:
    """The diary seams answer from the same code paths decide() sends."""

    def test_system_prompt_is_the_assembled_prompt(self):
        driver = AnthropicDriver(schemas={}, client=_StubClient(_response([])))
        assert driver.system_prompt("goal") == assembled_system_prompt("goal")

    def test_render_prompt_is_the_wire_messages_as_json(self):
        driver = AnthropicDriver(schemas={}, client=_StubClient(_response([])))
        transcript = [
            TranscriptEvent("user", "Who is Mira?"),
            TranscriptEvent("assistant", "Checking.", tool_calls=(
                ToolCall("get_node", {"node_id": "n1"}, id="toolu_1"),
            )),
            TranscriptEvent("tool", "Mira.", tool_name="get_node",
                            tool_use_id="toolu_1"),
        ]
        rendered = driver.render_prompt(transcript)
        assert json.loads(rendered) == messages_from_transcript(transcript)
