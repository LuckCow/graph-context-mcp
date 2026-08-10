"""The MCP wrappers stay in lockstep with the tool implementations.

Found live (WP15): `context` gained `text`/`detail` in ``tools.py`` but the
hand-written wrapper in ``server.py`` kept the old parameter list -- and the
tool schema is derived from the wrapper's signature, so an undeclared
argument **can never reach** the implementation: the LLM's `note` text
vanished without an error. The unit suite couldn't see
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

# ADR 042: the write tools keep the RETIRED params as explicit
# implementation-only arguments (an old-shape call gets a self-correcting
# redirect instead of an opaque error). They are deliberately absent from
# the MCP wrappers -- the schema must not advertise them.
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
        if p.name not in _RETIRED_IMPL_ONLY
    ]


@pytest.mark.parametrize("tool_name", TOOL_NAMES)
def test_wrapper_signature_matches_the_implementation(tool_name: str) -> None:
    wrapper = getattr(server, tool_name)
    implementation = getattr(tools, f"{tool_name}_tool")
    assert _api_params(wrapper, "ctx") == _api_params(implementation, "services"), (
        f"server.{tool_name} and tools.{tool_name}_tool have drifted; "
        "the tool schema comes from the wrapper's signature, so update the "
        "wrapper whenever the tool implementation's surface changes"
    )


async def test_ctx_never_reaches_the_published_tool_schema() -> None:
    """`ctx` is injected by the SDK, never asked of the model.

    The SDK picks the context parameter by ANNOTATION
    (``find_context_parameter``): an annotation it doesn't recognize
    silently demotes ``ctx`` to an ordinary tool argument, so the schema
    would ask the LLM to supply a Context it has no way to produce. That
    fails no import and no signature check -- only the published schema
    shows it. Pinned when mcp 2 made Context's lifespan type parameter
    default to ``dict[str, Any]`` and the annotations had to change.
    """
    published = await server.mcp.list_tools()
    assert {t.name for t in published} == set(TOOL_NAMES)
    leaked = [
        t.name for t in published
        if "ctx" in (t.input_schema or {}).get("properties", {})
    ]
    assert not leaked, (
        f"{leaked} advertise `ctx` as a tool parameter; the wrapper's "
        "Context annotation is no longer recognized by the MCP SDK"
    )


def test_wrappers_never_advertise_retired_params() -> None:
    for tool_name in ("create_node", "update_node"):
        wrapper_params = {
            p.name
            for p in inspect.signature(
                getattr(server, tool_name)
            ).parameters.values()
        }
        assert not wrapper_params & _RETIRED_IMPL_ONLY
