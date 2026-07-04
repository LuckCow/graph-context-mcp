"""Tool-layer tests (WP2/WP3): the three invariants and the tool policies.

These drive the plain async functions in ``interface/tools.py`` directly
(no MCP client), exactly as the demo script does. They pin the contracts
that the handoff worklist calls out: the header on every response, error
messages that echo allowed values, the default Prose/SessionContext
exclusion and ``only_stale`` narrowing.
"""

from __future__ import annotations

import pytest

from graph_context.application.capture_recorder import CaptureRecorder
from graph_context.interface import tools
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
    # Through the SERVICE: the record_prose tool was removed 2026-07-04
    # (capture is the orchestrator's job); the traversal-hiding policy
    # this section pins is about the nodes, not how they were made.
    recorder = CaptureRecorder(services.repository, now=lambda: "t")
    node = await recorder.record(
        text="A scene about the vaults.", summary="Scene.",
        references=list(node_ids),
    )
    return node.id


async def test_explore_excludes_prose_by_default(
    services: tools.Services, world: World
) -> None:
    await _record_prose_about(services, world.undercroft.id)
    out = await tools.explore_tool(services, start=world.undercroft.id, depth=1)
    assert "(Capture" not in _body(out)


async def test_explore_includes_prose_when_requested(
    services: tools.Services, world: World
) -> None:
    await _record_prose_about(services, world.undercroft.id)
    out = await tools.explore_tool(
        services, start=world.undercroft.id, depth=1,
        include_types=["Capture", "Location"],
    )
    assert "(Capture" in _body(out)


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


# -- WP11 (ADRs 014/016): semantic tier + resolver suggestions ---------------


async def _semantic_services(world: World) -> tools.Services:
    """Services over the standard world with the hash embedder wired."""
    from graph_context.application.ranker import Ranker
    from graph_context.application.semantic_projector import SemanticProjector
    from graph_context.domain.session import SessionState
    from graph_context.infrastructure.memory.fake_repository import (
        InMemoryGraphRepository,
    )
    from graph_context.infrastructure.semantic.hashing_embedder import (
        HashingEmbedder,
    )
    from graph_context.infrastructure.semantic.memory_index import (
        InMemorySemanticIndex,
    )

    # Rebuild a fresh world so this helper controls the whole stack.
    repository = InMemoryGraphRepository()
    from graph_context.application.node_writer import NodeWriter
    from graph_context.domain.models import LinkSpec, NodeDraft

    writer = NodeWriter(repository, SessionState())
    mira = await writer.create_node(NodeDraft(
        "Character", name="Mira", summary="Exiled siege engineer of Brakk.",
    ))
    await writer.create_node(
        NodeDraft("Item", name="Ashbrand", summary="A blade quenched in ash."),
        links=[LinkSpec("wielded_by", other=mira.id)],
    )
    embedder = HashingEmbedder()
    index = InMemorySemanticIndex()
    projector = SemanticProjector(repository, embedder, index)
    await projector.refresh()
    return tools.build_services(
        repository, SessionState(project="Ashfall"),
        projector=projector, ranker=Ranker(repository, embedder, index),
    )


async def test_find_node_semantic_tier_labels_hits_with_evidence(
    world: World,
) -> None:
    svc = await _semantic_services(world)
    body = _body(await tools.find_node_tool(
        svc, name="the exiled engineer of the siege"
    ))
    assert "semantic match(es)" in body   # labelled: the LLM holds fuzzy hits
    assert "Mira" in body
    assert "why:" in body                 # evidence rides along


async def test_find_node_exact_name_never_goes_semantic(world: World) -> None:
    svc = await _semantic_services(world)
    body = _body(await tools.find_node_tool(svc, name="Mira"))
    assert "semantic" not in body         # tier 1 answered; tier 3 never ran


async def test_resolver_miss_suggests_closest_by_meaning(world: World) -> None:
    svc = await _semantic_services(world)
    out = await tools.get_node_tool(svc, node_id="the exiled siege engineer")
    assert "ERROR:" in out
    assert "Closest by meaning:" in out
    mira = svc.repository.graph.resolve("Mira")
    assert mira.id in out                 # id ready to copy into the retry


async def test_mutations_are_never_fuzzily_resolved(world: World) -> None:
    """ADR 014 non-feature: a description on update_node SUGGESTS, only."""
    svc = await _semantic_services(world)
    mira = svc.repository.graph.resolve("Mira")
    out = await tools.update_node_tool(
        svc, node_id="the exiled siege engineer", summary="clobbered!",
    )
    assert "ERROR:" in out and "Closest by meaning:" in out
    assert svc.repository.graph.node(mira.id).summary != "clobbered!"


async def test_without_ranker_behavior_is_unchanged(
    services: tools.Services, world: World
) -> None:
    body = _body(await tools.find_node_tool(services, name="nobody here"))
    assert "no match" in body             # the tier degrades away (off)
    out = await tools.get_node_tool(services, node_id="nobody here")
    assert "ERROR:" in out and "Closest by meaning" not in out
