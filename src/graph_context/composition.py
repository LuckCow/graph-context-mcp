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
from graph_context.interface.profiles import DomainProfile
from graph_context.interface.tools import Services, build_services

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
        return build_services(
            InMemoryGraphRepository(role_overrides=profile.role_overrides),
            session,
            journal=journal,
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
    await ensure_schema(client)
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
    return build_services(
        repository, session, persister, journal=journal,
    ), teardown


async def run_teardown(teardown: list[TeardownHook]) -> None:
    """Run hooks in reverse registration order, never letting one abort the rest."""
    for hook in reversed(teardown):
        try:
            await hook()
        except Exception:  # noqa: BLE001
            logger.exception("teardown hook failed")
