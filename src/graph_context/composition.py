"""The shared service builder: one wiring, two composition roots (ADR 007).

Both the MCP server (``interface/server.py``) and the orchestrator's CLI
(``orchestrator/cli.py``) need the identical build:

    config -> client -> bootstrap -> repository -> hydrate -> session
    (restored via SessionStore) -> services

so it lives here exactly once. This module and the composition roots that
call it are the ONLY places allowed to import ``infrastructure`` (the
import-linter contracts name them); everything below stays wired to ports.

Environment surface (unchanged from the server's original wiring):

* ``GC_BACKEND``           -- ``anytype`` (default) or ``memory``.
* ``GC_PROJECT_NAME``      -- initial project label (cosmetic).
* ``GC_FIELD_DENYLIST``    -- ADR 012 field-reflection silences.
* ``GC_EMBEDDER``          -- ``off`` (default) or ``hash`` (deterministic,
                              model-free); real models arrive with the
                              container rebuild (ADR 014).
* ``GC_SEMANTIC_CACHE``    -- embedding-cache directory (default
                              ``~/.cache/graph-context``); files are
                              disposable projections.

(``GC_STORE_LLM_INPUT`` moved to the orchestrator's root: WP7 retired
record_prose's llm_* parameters, so the knob now governs intent-node
prompt storage instead.)
* Anytype connection env is read by ``AnytypeConfig.from_env``.

The active :class:`~graph_context.interface.profiles.DomainProfile` is a
parameter, not an env read: each root resolves ``GC_PROFILE`` itself (the
MCP server must do so at import time, when tool docstrings register).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Awaitable, Callable

from graph_context.application.mutation_journal import MutationJournal
from graph_context.application.semantic_projector import SemanticProjector
from graph_context.interface.profiles import DomainProfile
from graph_context.interface.tools import Services, build_services
from graph_context.ports.graph_repository import GraphRepository
from graph_context.ports.semantic import Embedder, SemanticIndex

logger = logging.getLogger(__name__)

TeardownHook = Callable[[], Awaitable[None]]


async def build_runtime(
    profile: DomainProfile,
    *,
    journal: MutationJournal | None = None,
) -> tuple[Services, list[TeardownHook]]:
    """Build the full service bundle for one process.

    Returns the services plus teardown hooks to run in reverse order on
    shutdown (session flush before client close).
    """
    from graph_context.domain.session import SessionState

    backend = os.environ.get("GC_BACKEND", "anytype")
    session = SessionState(project=os.environ.get("GC_PROJECT_NAME"))
    teardown: list[TeardownHook] = []

    logger.info("profile=%s (%s)", profile.name, profile.description)
    if backend == "memory":
        from graph_context.infrastructure.memory.fake_repository import (
            InMemoryGraphRepository,
        )

        logger.info("backend=memory (development mode; nothing persists)")
        memory_repo = InMemoryGraphRepository(role_overrides=profile.role_overrides)
        return build_services(
            memory_repo,
            session,
            journal=journal,
            projector=await _build_semantic(memory_repo, space_id=None),
        ), teardown

    from graph_context.application.session_persister import SessionPersister
    from graph_context.infrastructure.anytype.client import AnytypeClient
    from graph_context.infrastructure.anytype.config import AnytypeConfig
    from graph_context.infrastructure.anytype.repository import AnytypeGraphRepository
    from graph_context.infrastructure.anytype.schema_bootstrap import ensure_schema
    from graph_context.infrastructure.anytype.session_repository import (
        AnytypeSessionStore,
    )

    config = AnytypeConfig.from_env()
    client = AnytypeClient(config)
    teardown.append(client.aclose)
    timeline = (profile.time_property, profile.time_format)  # ADR 015
    await ensure_schema(client, timeline=timeline)
    # GC_FIELD_DENYLIST (ADR 012): comma-separated property keys to hide
    # from field reflection, on top of the built-in system-noise denylist.
    field_denylist = frozenset(
        key.strip()
        for key in os.environ.get("GC_FIELD_DENYLIST", "").split(",")
        if key.strip()
    )
    repository = AnytypeGraphRepository(
        client,
        role_overrides=profile.role_overrides,
        field_denylist=field_denylist,
        timeline=timeline,
    )
    await repository.hydrate()
    logger.info(
        "hydrated space %s: %d nodes / %d edges",
        config.space_id,
        repository.graph.node_count(),
        repository.graph.edge_count(),
    )
    # Restore the working session from the SessionContext meta-node, and
    # arrange a debounced flush (note_mutation in tools) + a final flush on
    # shutdown. A corrupt/missing snapshot degrades to the fresh session.
    store = AnytypeSessionStore(client)
    session = await SessionPersister.load_or_fresh(store, session)
    if not session.project:
        # Derived cosmetic default: the space's own name. Never blocks startup;
        # GC_PROJECT_NAME and a persisted set_project both take precedence.
        try:
            session.project = (await client.get_space()).get("name") or None
        except Exception:  # noqa: BLE001
            logger.warning("could not read space name for the project label")
    persister = SessionPersister(store, session)
    teardown.append(persister.flush)  # flush on shutdown (LIFO: before aclose)
    projector = await _build_semantic(repository, space_id=config.space_id)
    return build_services(
        repository, session, persister, journal=journal, projector=projector,
    ), teardown


def _make_embedder(choice: str) -> Embedder | None:
    """GC_EMBEDDER resolution; unknown values fail loudly at startup."""
    if choice in {"", "off", "0", "none"}:
        return None
    if choice == "hash":
        from graph_context.infrastructure.semantic.hashing_embedder import (
            HashingEmbedder,
        )

        return HashingEmbedder()
    raise ValueError(
        f"unknown GC_EMBEDDER {choice!r}; allowed: off, hash "
        "(model-backed embedders arrive with the container rebuild)"
    )


async def _build_semantic(
    repository: GraphRepository, *, space_id: str | None
) -> SemanticProjector | None:
    """The semantic projection (ADR 014), or None when GC_EMBEDDER=off.

    Anytype spaces get the persistent SQLite cache (one file per space +
    model); the memory backend keeps the cache in memory too -- nothing
    about it should outlive a store that itself evaporates.
    """
    embedder = _make_embedder(os.environ.get("GC_EMBEDDER", "off").strip().lower())
    if embedder is None:
        return None
    index: SemanticIndex
    if space_id is None:
        from graph_context.infrastructure.semantic.memory_index import (
            InMemorySemanticIndex,
        )

        index = InMemorySemanticIndex()
    else:
        from pathlib import Path

        from graph_context.infrastructure.semantic.sqlite_index import (
            SqliteSemanticIndex,
        )

        cache_dir = Path(os.environ.get(
            "GC_SEMANTIC_CACHE", str(Path.home() / ".cache" / "graph-context")
        ))
        index = SqliteSemanticIndex(
            cache_dir / f"semantic-{space_id}.sqlite", model=embedder.model_id
        )
    projector = SemanticProjector(repository, embedder, index)
    embedded = await projector.refresh()  # full pass: seed + prune
    logger.info("semantic projection ready (%d embedded at startup)", embedded)
    return projector


async def run_teardown(teardown: list[TeardownHook]) -> None:
    """Run hooks in reverse registration order, never letting one abort the rest."""
    for hook in reversed(teardown):
        try:
            await hook()
        except Exception:  # noqa: BLE001
            logger.exception("teardown hook failed")
