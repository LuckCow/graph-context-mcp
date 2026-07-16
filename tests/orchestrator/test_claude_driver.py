"""ClaudeAgentDriver translation seams (WP6).

The SDK round-trip is covered by the gated live test
(``tests/e2e/test_live_claude_driver.py``) and the demo script; here the
pure translation logic is pinned. Self-skips where claude-agent-sdk is
not installed (CI installs only ``[dev]``; the ``[orchestrator]`` extra
rides the devcontainer image).
"""

from __future__ import annotations

import pytest

pytest.importorskip("claude_agent_sdk")

from claude_agent_sdk import ResultMessage  # noqa: E402

from graph_context.orchestrator import modes  # noqa: E402
from graph_context.orchestrator.claude_driver import (  # noqa: E402
    WEB_SEARCH_TOOL,
    derive_schema,
    local_tool_name,
    permission_gate,
    sdk_tools,
    session_options,
    usage_from_result,
)

# Transcript rendering is SDK-free and pinned in test_driver_common (CI
# runs it there; this module self-skips without claude-agent-sdk).


class TestSchemaDerivation:
    """Schemas come from the wrappers' signatures -- one source of truth.

    ``additionalProperties: false`` is the load-bearing bit: with an open
    schema the live model echoed the schema's own keys back as arguments.
    """

    def test_a_representative_signature_derives_fully(self):
        async def sample(
            services,
            name: str,
            depth: int = 1,
            weight: float | str | None = None,
            tags: list[str] | None = None,
            fields: dict[str, str] | None = None,
            strict: bool = False,
        ) -> str:
            return ""

        schema = derive_schema(sample)
        assert schema["additionalProperties"] is False
        assert schema["required"] == ["name"]
        p = schema["properties"]
        assert "services" not in p
        assert p["name"] == {"type": "string"}
        assert p["depth"] == {"type": "integer"}
        assert p["weight"] == {"type": ["number", "string"]}
        assert p["tags"] == {"type": "array", "items": {"type": "string"}}
        assert p["fields"] == {"type": "object"}
        assert p["strict"] == {"type": "boolean"}

    def test_every_surface_tool_derives_a_closed_object_schema(self):
        for name, fn in modes.full_surface().items():
            schema = derive_schema(fn)
            assert schema["type"] == "object", name
            assert schema["additionalProperties"] is False, name
            assert schema["properties"], name


class TestToolRegistration:
    def test_one_tool_per_binding_entry_with_its_derived_schema(self):
        schemas = {"get_node": derive_schema(modes.full_surface()["get_node"])}
        tools = sdk_tools(
            {"get_node": "Fetch one node.", "explore": "Walk the graph."}, schemas
        )
        assert [t.name for t in tools] == ["explore", "get_node"]
        assert tools[1].description == "Fetch one node."
        assert tools[1].input_schema["additionalProperties"] is False
        assert "node_id" in tools[1].input_schema["properties"]
        # A name without a schema degrades to a bare object, never an
        # additionalProperties-bearing one (the live schema-echo lesson).
        assert tools[0].input_schema == {"type": "object"}

    def test_sdk_tool_names_strip_back_to_binding_names(self):
        assert local_tool_name("mcp__gc__get_node") == "get_node"
        # Defensive: an already-local name passes through unchanged.
        assert local_tool_name("get_node") == "get_node"


class TestSessionCapabilityBoundary:
    """The binding is the WHOLE surface (ADR 007): no Claude Code
    built-ins (Read, Write, Bash, ...), no filesystem settings that could
    smuggle extra MCP servers or permissions into the session."""

    def _options(self, **kwargs):
        async def deny(name, tool_input, context):  # pragma: no cover
            raise AssertionError("never invoked here")

        server = {"type": "sdk", "name": "gc", "instance": object()}
        return session_options(
            server, "goal", model=None, effort=None, can_use_tool=deny,
            cli_path=None, **kwargs,
        )

    def test_builtin_tools_are_disabled_by_the_empty_list(self):
        options = self._options()
        # [] means "no built-ins"; None would mean "the CLI's full default
        # toolset" -- the empty list is the entire boundary here.
        assert options.tools == []
        assert options.tools is not None

    def test_only_the_gc_server_is_registered(self):
        assert list(self._options().mcp_servers) == ["gc"]

    def test_filesystem_settings_are_not_loaded(self):
        # None = load user+project+local settings (CLI default), which can
        # inject MCP servers, permission grants, and hooks. [] = isolation.
        assert self._options().setting_sources == []

    def test_web_search_admits_exactly_the_websearch_builtin(self):
        # ADR 030: the one mode-gated exception to the empty list --
        # server-side execution, so the host boundary is intact.
        options = self._options(web_search=True)
        assert options.tools == [WEB_SEARCH_TOOL]
        assert options.setting_sources == []  # isolation unchanged


class TestPermissionGate:
    """Deny-all with the one ADR 030 exception: WebSearch, when the mode
    admits it, runs server-side INSIDE the session."""

    @staticmethod
    async def _ask(gate, name):
        return await gate(name, {}, None)

    async def test_graph_tools_are_always_denied_with_interrupt(self):
        from claude_agent_sdk import PermissionResultDeny

        for enabled in (False, True):
            result = await self._ask(
                permission_gate(enabled), "mcp__gc__get_node"
            )
            assert isinstance(result, PermissionResultDeny)
            assert result.interrupt is True

    async def test_websearch_is_allowed_only_when_the_mode_admits_it(self):
        from claude_agent_sdk import (
            PermissionResultAllow,
            PermissionResultDeny,
        )

        allowed = await self._ask(permission_gate(True), WEB_SEARCH_TOOL)
        assert isinstance(allowed, PermissionResultAllow)
        denied = await self._ask(permission_gate(False), WEB_SEARCH_TOOL)
        assert isinstance(denied, PermissionResultDeny)


class TestUsageTranslation:
    """ResultMessage -> DecideUsage: the eval harness's metrics tap."""

    @staticmethod
    def _result(**overrides):
        base = dict(
            subtype="success", duration_ms=4200, duration_api_ms=3100,
            is_error=False, num_turns=2, session_id="s",
            total_cost_usd=0.0123,
            usage={
                "input_tokens": 11, "output_tokens": 220,
                "cache_read_input_tokens": 3000,
                "cache_creation_input_tokens": 450,
            },
        )
        base.update(overrides)
        return ResultMessage(**base)

    def test_the_usage_block_maps_field_for_field(self):
        usage = usage_from_result(self._result())
        assert usage.duration_ms == 4200
        assert usage.duration_api_ms == 3100
        assert usage.total_cost_usd == 0.0123
        assert usage.input_tokens == 11
        assert usage.output_tokens == 220
        assert usage.cache_read_tokens == 3000
        assert usage.cache_creation_tokens == 450
        assert usage.num_turns == 2

    def test_missing_usage_degrades_to_zeroes_never_raises(self):
        usage = usage_from_result(self._result(usage=None, total_cost_usd=None))
        assert usage.output_tokens == 0
        assert usage.cache_read_tokens == 0
        assert usage.total_cost_usd is None


class TestBlockHarvest:
    """WP22: streamed blocks -> the decision, server searches captured
    with their raw results (both wire shapes the CLI has shown)."""

    def test_native_server_blocks_pair_call_and_result(self) -> None:
        import json

        from claude_agent_sdk import (
            ServerToolResultBlock,
            ServerToolUseBlock,
            TextBlock,
        )

        from graph_context.orchestrator.claude_driver import (
            BlockHarvest,
            harvest_assistant_blocks,
        )

        harvest = BlockHarvest()
        harvest_assistant_blocks([
            ServerToolUseBlock(id="s1", name="WebSearch",
                               input={"query": "anytype api"}),
            ServerToolResultBlock(tool_use_id="s1", content={
                "type": "web_search_tool_result",
                "content": [{"title": "A", "url": "https://a"}],
            }),
            TextBlock(text="Answer."),
        ], harvest)
        turn = harvest.turn()
        assert turn.reply == "Answer."
        assert turn.tool_calls == ()
        (call,) = turn.server_tool_calls
        assert call.name == "WebSearch" and call.id == "s1"
        (raw,) = turn.server_tool_results
        assert json.loads(raw)["content"]["content"][0]["title"] == "A"

    def test_tooluseblock_websearch_result_arrives_in_a_user_message(
        self,
    ) -> None:
        import json

        from claude_agent_sdk import ToolResultBlock, ToolUseBlock

        from graph_context.orchestrator.claude_driver import (
            BlockHarvest,
            harvest_assistant_blocks,
            harvest_result_blocks,
        )

        harvest = BlockHarvest()
        harvest_assistant_blocks([
            ToolUseBlock(id="s1", name="WebSearch",
                         input={"query": "anytype api"}),
        ], harvest)
        harvest_result_blocks([
            ToolResultBlock(tool_use_id="s1",
                            content="Web search results: ..."),
            ToolResultBlock(tool_use_id="other",  # not a search of ours
                            content="ignored"),
        ], harvest)
        turn = harvest.turn()
        assert turn.tool_calls == ()  # never pipeline work
        (raw,) = turn.server_tool_results
        assert json.loads(raw)["content"].startswith("Web search results")

    def test_local_tool_calls_are_untouched_by_the_harvest_split(
        self,
    ) -> None:
        from claude_agent_sdk import ToolUseBlock

        from graph_context.orchestrator.claude_driver import (
            BlockHarvest,
            harvest_assistant_blocks,
        )

        harvest = BlockHarvest()
        harvest_assistant_blocks([
            ToolUseBlock(id="t1", name="mcp__gc__find_node",
                         input={"name": "Mira"}),
        ], harvest)
        turn = harvest.turn()
        assert turn.server_tool_calls == ()
        (call,) = turn.tool_calls
        assert call.name == "find_node"  # prefix stripped
