"""The ranking eval golden (ADR 016): weights answer to this, not vibes.

Every case in ranking_eval.toml must place its expected node in the
TOP-3 for its query. Tuning RankingWeights is legitimate exactly when
this suite stays green -- and extending the eval file IS the review
artifact for ranking changes, like docstring goldens are for prompts.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from graph_context.application.capture_recorder import CaptureRecorder
from graph_context.application.ranker import Ranker
from graph_context.application.semantic_projector import SemanticProjector
from graph_context.domain.models import LinkSpec, NodeDraft
from graph_context.infrastructure.memory.fake_repository import InMemoryGraphRepository
from graph_context.infrastructure.semantic.hashing_embedder import HashingEmbedder
from graph_context.infrastructure.semantic.memory_index import InMemorySemanticIndex

_EVAL = Path(__file__).parent / "ranking_eval.toml"
CASES = tomllib.loads(_EVAL.read_text())["case"]


async def _eval_world() -> InMemoryGraphRepository:
    """A small but representative world; grow it WITH the eval file."""
    r = InMemoryGraphRepository()
    mira = await r.create_node(NodeDraft(
        "Character", name="Mira", summary="Exiled siege engineer of Brakk.",
    ))
    undercroft = await r.create_node(NodeDraft(
        "Location", name="The Undercroft", summary="Vaults beneath the city of Brakk.",
    ))
    siege = await r.create_node(
        NodeDraft("Event", name="Siege of Brakk", story_time=10,
                  summary="The year-long siege in which the city fell."),
        links=[
            LinkSpec("participated_in", other=mira.id, outgoing=False),
            LinkSpec("located_at", other=undercroft.id),
        ],
    )
    await r.create_node(
        NodeDraft("Item", name="Ashbrand", summary="A blade quenched in ash."),
        links=[
            LinkSpec("wielded_by", other=mira.id),
            LinkSpec("used_in", other=siege.id),
        ],
    )
    await r.create_node(
        NodeDraft("Character", name="Orla", summary="A smuggler and ally."),
        links=[LinkSpec("knows", other=mira.id)],
    )
    renata = await r.create_node(NodeDraft(
        "Character", name="Renata Voss",
        summary="Senior product executive at Argus Systems.",
    ))
    await r.create_node(
        NodeDraft("Organization", name="Argus Systems", summary="A megacorp."),
        links=[LinkSpec("member_of", other=renata.id, outgoing=False)],
    )
    capture = CaptureRecorder(r, now=lambda: "t")
    await capture.record(
        text="Scene", summary="s", references=[mira.id, undercroft.id],
        title="The vaults fall",
    )
    return r


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["query"][:40])
async def test_expected_node_in_top_k(case: dict) -> None:
    repository = await _eval_world()
    embedder = HashingEmbedder()
    index = InMemorySemanticIndex()
    await SemanticProjector(repository, embedder, index).refresh()
    ranker = Ranker(repository, embedder, index)
    k = case.get("k", 3)
    hits = await ranker.rank(case["query"], limit=k)
    names = [hit.node.name for hit in hits]
    assert case["expect"] in names, (
        f"{case['query']!r} -> expected {case['expect']!r} in top-{k}, got {names}"
    )
