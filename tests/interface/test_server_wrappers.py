"""The FastMCP wrappers stay in lockstep with the tool implementations.

Found live (WP15): `context` gained `text`/`detail` in ``tools.py`` but the
hand-written wrapper in ``server.py`` kept the old parameter list -- and
FastMCP **silently drops** arguments the wrapper doesn't declare, so the
LLM's `note` text vanished without an error. The unit suite couldn't see
it because it drives ``tools.py`` directly; only the real MCP surface
showed it. This test makes the drift unrepresentable: every parameter of
each tool implementation (minus ``services``) must appear on its wrapper
(minus ``ctx``) with the same name, order, default, and annotation.

(Importing ``interface.server`` here is fine: tests, like the composition
root, may touch the MCP SDK.)
"""

from __future__ import annotations

import inspect

import pytest

from graph_context.interface import server, tools
from graph_context.interface.profiles import TOOL_NAMES


def _api_params(fn: object, skip_first: str) -> list[tuple[str, object, str]]:
    signature = inspect.signature(fn)  # type: ignore[arg-type]
    params = list(signature.parameters.values())
    assert params and params[0].name == skip_first, (
        f"{fn} must take {skip_first!r} first, got {params[:1]}"
    )
    return [
        (p.name, p.default, " ".join(str(p.annotation).split()))
        for p in params[1:]
    ]


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_wrapper_signature_matches_the_implementation(tool_name: str) -> None:
    wrapper = getattr(server, tool_name)
    implementation = getattr(tools, f"{tool_name}_tool")
    assert _api_params(wrapper, "ctx") == _api_params(implementation, "services"), (
        f"server.{tool_name} and tools.{tool_name}_tool have drifted; "
        "FastMCP silently drops undeclared arguments, so update the "
        "wrapper whenever the tool implementation's surface changes"
    )
