"""Tool-layer tests (WP2/WP3): the three invariants and the tool policies.

These drive the plain async functions in ``interface/tools.py`` directly
(no MCP client), exactly as the demo script does. They pin the contracts
that the handoff worklist calls out: the header on every response, error
messages that echo allowed values, the default Prose/SessionContext
exclusion, ``only_stale`` narrowing, and ``include_prose`` excerpts.
"""

from __future__ import annotations

import pytest

from graph_context.interface import tools
from graph_context.interface.presenters import PROSE_EXCERPT_CHARS
from tests.conftest import World

pytestmark = pytest.mark.usefixtures("world")


def _header_ok(out: str) -> bool:
    return out.startswith("[project: Ashfall | focus:")


def _body(out: str) -> str:
    """The response minus its first-line context header (which itself lists
    focus/recent node names and so must not be matched against)."""
    return out.split("\n", 1)[1] if "\n" in out else ""


# -- invariant 1: the header is on every response, success AND error --------


async def test_header_present_on_success(services: tools.Services, world: World) -> None:
    out = await tools.get_node_tool(services, node_id=world.mira.id)
    assert _header_ok(out)
    assert "Mira" in out


async def test_header_present_on_error(services: tools.Services) -> None:
    out = await tools.get_node_tool(services, node_id="does-not-exist")
    assert _header_ok(out)
    assert "ERROR:" in out


# -- invariant 2: errors are prompts -- parse-level validation --------------
# (The type/relation vocabulary is OPEN; "does this type/relation exist in
# the space?" is enforced by the Anytype repository and tested in
# tests/anytype/test_repository.py. The tool layer validates shape only.)


async def test_empty_node_type_errors(services: tools.Services) -> None:
    out = await tools.create_node_tool(services, type="   ", name="x", summary="y")
    assert "ERROR:" in out
    assert "type" in out


async def test_empty_edge_label_errors(
    services: tools.Services, world: World
) -> None:
    out = await tools.create_node_tool(
        services, type="Item", name="Relic", summary="s",
        links=[{"edge_type": "", "other": world.mira.id}],
    )
    assert "ERROR:" in out
    assert "edge_type" in out


async def test_bad_detail_lists_allowed_levels(
    services: tools.Services, world: World
) -> None:
    out = await tools.explore_tool(services, start=world.mira.id, detail="terse")
    assert "ERROR:" in out
    assert "names" in out and "summaries" in out and "full" in out


async def test_malformed_link_names_required_keys(
    services: tools.Services, world: World
) -> None:
    out = await tools.create_node_tool(
        services, type="Item", name="Relic", summary="s",
        links=[{"edge_type": "possesses"}],  # missing 'other'
    )
    assert "ERROR:" in out
    assert "edge_type" in out and "other" in out


# -- invariant 3 / WP2 policy: Prose & SessionContext hidden by default ------


async def _record_prose_about(services: tools.Services, *node_ids: str) -> str:
    out = await tools.record_prose_tool(
        services, text="A scene about the vaults.", summary="Scene.",
        references=list(node_ids),
    )
    # id appears inline: "recorded prose '...' (id=<id>) ..."
    return out.split("id=", 1)[1].split(")", 1)[0]


async def test_explore_excludes_prose_by_default(
    services: tools.Services, world: World
) -> None:
    await _record_prose_about(services, world.undercroft.id)
    out = await tools.explore_tool(services, start=world.undercroft.id, depth=1)
    assert "(Prose" not in _body(out)


async def test_explore_includes_prose_when_requested(
    services: tools.Services, world: World
) -> None:
    await _record_prose_about(services, world.undercroft.id)
    out = await tools.explore_tool(
        services, start=world.undercroft.id, depth=1,
        include_types=["Prose", "Location"],
    )
    assert "(Prose" in _body(out)


# -- WP3 stale-summary workflow: only_stale narrows (start exempt) -----------


async def test_only_stale_narrows_to_flagged_nodes(
    services: tools.Services, world: World
) -> None:
    # An update without a fresh summary flags the Event as stale.
    await tools.update_node_tool(services, node_id=world.fall.id, description="razed")
    out = _body(await tools.explore_tool(
        services, start=world.mira.id, depth=2, only_stale=True, detail="names"
    ))
    assert "Fall of Brakk" in out            # stale -> kept
    assert "The Undercroft" not in out       # not stale -> dropped
    assert "Mira" in out                     # start node -> always kept


# -- WP3 get_node include_prose: excerpt is bounded and marked --------------


async def test_include_prose_excerpt_is_capped(
    services: tools.Services, world: World
) -> None:
    long_text = "x" * (PROSE_EXCERPT_CHARS + 500)
    await tools.record_prose_tool(
        services, text=long_text, summary="Long scene.",
        references=[world.undercroft.id],
    )
    body = _body(await tools.get_node_tool(
        services, node_id=world.undercroft.id, include_prose=1
    ))
    assert "prose (1 of 1):" in body
    # the rendered excerpt is truncated with an ellipsis marker
    assert "…" in body
    # no full body leaks: the run of x's never reaches the untruncated length
    assert "x" * (PROSE_EXCERPT_CHARS + 1) not in body


async def test_include_prose_default_zero_shows_count_but_no_excerpts(
    services: tools.Services, world: World
) -> None:
    await tools.record_prose_tool(
        services, text="Vault scene.\nThe hidden second line.", summary="Scene.",
        references=[world.undercroft.id],
    )
    body = _body(await tools.get_node_tool(services, node_id=world.undercroft.id))
    assert "prose: 1 passage(s) reference this node" in body
    assert "include_prose" in body  # the hint tells the LLM how to fetch them
    assert "The hidden second line." not in body  # no excerpt/body leaked


# -- record_prose requires explicit provenance ------------------------------


async def test_record_prose_requires_references(services: tools.Services) -> None:
    out = await tools.record_prose_tool(
        services, text="orphan", summary="s", references=[]
    )
    assert "ERROR:" in out
    assert "reference" in out.lower()


# -- context tool: stats and resync reporting -------------------------------


async def test_context_get_reports_stats(services: tools.Services, world: World) -> None:
    out = await tools.context_tool(services, action="get")
    assert "nodes" in out and "edges" in out and "stale" in out
    assert "overview" in out  # self-correcting pointer to entry-point discovery


async def test_context_unknown_action_lists_actions(services: tools.Services) -> None:
    out = await tools.context_tool(services, action="teleport")
    assert "ERROR:" in out
    for verb in ("get", "overview", "resync", "focus", "pin", "unpin", "remove", "clear"):
        assert verb in out


# -- cold-start discovery: context overview is the entry point --------------


async def test_context_overview_lists_entry_points(
    services: tools.Services, world: World
) -> None:
    body = _body(await tools.context_tool(services, action="overview"))
    # Mira is the highest-degree node in the fixture (4 incident edges).
    assert world.mira.id in body
    assert "types:" in body and "Character" in body


async def test_context_overview_map_alias(
    services: tools.Services, world: World
) -> None:
    body = _body(await tools.context_tool(services, action="map"))
    assert world.mira.id in body


async def test_empty_focus_error_names_overview(services: tools.Services) -> None:
    # An empty focus stack (as in a fresh session) has nothing for the start
    # default to resolve to -- the error must point at the way out.
    await tools.context_tool(services, action="clear")
    out = await tools.explore_tool(services, start="")
    assert "ERROR:" in out
    assert "overview" in out


# -- name resolution: tools accept a name anywhere an id is expected --------


async def test_get_node_accepts_a_name(
    services: tools.Services, world: World
) -> None:
    # The transcript's failing call -- get_node by name -- now resolves.
    out = await tools.get_node_tool(services, node_id="Mira")
    assert _header_ok(out)
    assert f"id={world.mira.id}" in out


async def test_get_node_name_is_case_insensitive(services: tools.Services) -> None:
    out = await tools.get_node_tool(services, node_id="the undercroft")
    assert "The Undercroft" in _body(out)


async def test_explore_start_accepts_a_name(
    services: tools.Services, world: World
) -> None:
    out = await tools.explore_tool(services, start="Mira", depth=1)
    assert world.mira.id in _body(out)


async def test_find_path_endpoints_accept_names(
    services: tools.Services, world: World
) -> None:
    out = await tools.find_path_tool(services, start="Mira", target="Ashbrand")
    assert "Mira" in _body(out) and "Ashbrand" in _body(out)


async def test_create_node_link_other_accepts_a_name(
    services: tools.Services, world: World
) -> None:
    # Build the FamiLinc-style case: link a new node to an existing one by name.
    out = await tools.create_node_tool(
        services, type="Item", name="Relic", summary="A found thing.",
        links=[{"edge_type": "possesses", "other": "Mira", "outgoing": False}],
    )
    assert "ERROR:" not in out
    assert f"id={world.mira.id}" in out  # neighbour rendered with its real id


async def test_ambiguous_name_reports_candidates(
    services: tools.Services, world: World
) -> None:
    # "Brakk" matches both "Siege of Brakk" and "Fall of Brakk".
    out = await tools.get_node_tool(services, node_id="Brakk")
    assert "ERROR:" in out
    assert world.siege.id in out and world.fall.id in out


async def test_unknown_name_points_at_discovery(services: tools.Services) -> None:
    out = await tools.get_node_tool(services, node_id="Nonexistent Person")
    assert "ERROR:" in out
    assert "find_node" in out or "overview" in out


# -- find_node tool: explicit name search -----------------------------------


async def test_find_node_lists_matches_with_ids(
    services: tools.Services, world: World
) -> None:
    body = _body(await tools.find_node_tool(services, name="Brakk"))
    assert world.siege.id in body and world.fall.id in body


async def test_find_node_type_filter(
    services: tools.Services, world: World
) -> None:
    body = _body(await tools.find_node_tool(services, name="Brakk", type="Event"))
    assert world.siege.id in body and world.fall.id in body
    body_loc = _body(await tools.find_node_tool(services, name="Brakk", type="Location"))
    assert "no match" in body_loc


async def test_find_node_no_match_points_at_overview(
    services: tools.Services,
) -> None:
    body = _body(await tools.find_node_tool(services, name="zzz nobody"))
    assert "overview" in body
