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

# ADR 042: the write tools ABSORB the retired params through a **kwargs
# catch-all rather than declaring them (an old-shape call gets a
# self-correcting redirect instead of an opaque error). A catch-all
# declares no argument, so it is not part of either surface.
_RETIRED_IMPL_ONLY = frozenset(tools._RETIRED_WRITE_PARAMS)


def _api_params(fn: object, skip_first: str) -> list[tuple[str, object, str]]:
    signature = inspect.signature(fn)  # type: ignore[arg-type]
    params = list(signature.parameters.values())
    assert params and params[0].name == skip_first, (
        f"{fn} must take {skip_first!r} first, got {params[:1]}"
    )
    return [
        (p.name, p.default, " ".join(str(p.annotation).split()))
        for p in params[1:]
        if p.kind is not inspect.Parameter.VAR_KEYWORD
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


def test_neither_surface_advertises_retired_params() -> None:
    """ADR 042's retired names must reach neither the wrapper nor the
    implementation signature: both are published surfaces (the MCP
    schema comes from one, the drivers' derived schemas from the other),
    and a published retired param teaches the model to send it."""
    for tool_name in ("create_node", "update_node"):
        for fn in (getattr(server, tool_name), getattr(tools, f"{tool_name}_tool")):
            declared = {
                p.name
                for p in inspect.signature(fn).parameters.values()
                if p.kind is not inspect.Parameter.VAR_KEYWORD
            }
            assert not declared & _RETIRED_IMPL_ONLY, fn


async def test_ctx_never_reaches_the_published_tool_schema() -> None:
    """`ctx` is injected by the SDK, never asked of the model.

    FastMCP picks the context parameter by ANNOTATION: an annotation it
    doesn't recognize silently demotes ``ctx`` to an ordinary tool
    argument, so the schema would ask the LLM to supply a Context it has
    no way to produce. That fails no import and no signature check --
    only the published schema shows it. Worth pinning because the
    annotation is exactly what a version migration rewrites: the mcp 2
    attempt reparametrized every one of these, and the revert put them
    back.
    """
    published = await server.mcp.list_tools()
    assert {t.name for t in published} == set(TOOL_NAMES)
    leaked = [
        t.name for t in published
        if "ctx" in (t.inputSchema or {}).get("properties", {})
    ]
    assert not leaked, (
        f"{leaked} advertise `ctx` as a tool parameter; the wrapper's "
        "Context annotation is no longer recognized by the MCP SDK"
    )
