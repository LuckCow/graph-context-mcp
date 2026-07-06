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

from graph_context.orchestrator import modes  # noqa: E402
from graph_context.orchestrator.claude_driver import (  # noqa: E402
    derive_schema,
    local_tool_name,
    render_transcript,
    sdk_tools,
    session_options,
)
from graph_context.orchestrator.drivers import TranscriptEvent  # noqa: E402


class TestTranscriptRendering:
    def test_the_user_message_renders_plain(self):
        prompt = render_transcript([TranscriptEvent("user", "Who is Mira?")])
        assert prompt == "Who is Mira?"

    def test_tool_results_are_fenced_and_named(self):
        prompt = render_transcript([
            TranscriptEvent("user", "Who is Mira?"),
            TranscriptEvent("tool", "Mira: exiled engineer.", tool_name="get_node"),
        ])
        assert prompt.startswith("Who is Mira?")
        assert '<tool_result tool="get_node">' in prompt
        assert "Mira: exiled engineer." in prompt

    def test_prior_assistant_text_is_marked_as_earlier(self):
        prompt = render_transcript([
            TranscriptEvent("assistant", "I looked her up already."),
        ])
        assert "<assistant_earlier>" in prompt


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

    def _options(self):
        async def deny(name, tool_input, context):  # pragma: no cover
            raise AssertionError("never invoked here")

        server = {"type": "sdk", "name": "gc", "instance": object()}
        return session_options(
            server, "goal", model=None, effort=None, can_use_tool=deny,
            cli_path=None,
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
