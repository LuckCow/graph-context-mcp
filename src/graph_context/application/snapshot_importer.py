"""Use-case: reconcile the graph against an external system's exports
(WP54, ADR 062).

The importer is the write side of "the game is the ground truth". Each
tick it reads the source's current documents, maps them through the
format into desired nodes (``domain/imports.py`` + a format such as
``domain/runescape.py``), plans against the LIVE index, and applies:
creates for nodes the graph lacks, change-only updates for values that
differ. It runs as the FIRST listener of the ADR 044 change tick, so
the rule engine's diff on the same tick sees the game's values -- and
never a human's edit to an imported node, which this pass has already
written back.

Writes go straight to the repository (no ``NodeWriter``): imported nodes
are native types, but their values are the game's facts, not authored
content -- no summary-staleness rule, no focus touch, no journal (an
import is not a turn; the rule engine's tick writes are unjournalled for
the same reason). One failed write is logged and the pass continues;
the next tick retries, because the plan is recomputed from the index.

Types are the format's: :meth:`ensure_types` mints each missing one
through the ADR 041 port methods (``create_type`` / the retry-safe
``add_type_properties``), so a space gains the vocabulary once and a
new property in a later plugin version retrofits on the next start.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from graph_context.domain import imports
from graph_context.domain.imports import (
    ImportDocumentError,
    ImportFormat,
    ImportNode,
    ImportPlan,
)
from graph_context.domain.models import NodeDraft
from graph_context.errors import GraphContextError
from graph_context.ports.graph_repository import GraphRepository
from graph_context.ports.import_source import ImportSource

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImportReport:
    """What one pass did. ``failed`` carries per-node messages."""

    documents: int = 0
    created: int = 0
    updated: int = 0
    failed: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()  # documents the format rejected

    @property
    def wrote(self) -> bool:
        return bool(self.created or self.updated)


class SnapshotImporter:
    """Reconcile the repository against ``source`` under ``fmt``."""

    def __init__(
        self,
        repository: GraphRepository,
        source: ImportSource,
        fmt: ImportFormat,
    ) -> None:
        self._repository = repository
        self._source = source
        self._format = fmt

    async def ensure_types(self) -> tuple[str, ...]:
        """Mint the format's types the space lacks; retrofit properties
        onto the ones it has. Returns the names created. Loud on
        conflict (a same-name property with another format is a
        misconfigured space, not something to paper over)."""
        known = {name.casefold() for name in self._repository.known_node_types()}
        created: list[str] = []
        for spec in self._format.types:
            if spec.name.casefold() in known:
                _, attached = await self._repository.add_type_properties(
                    spec.name, spec.properties
                )
                if attached:
                    logger.info(
                        "import type %s gained %s", spec.name, ", ".join(attached)
                    )
                continue
            await self._repository.create_type(
                spec.name, plural=spec.plural, properties=spec.properties
            )
            created.append(spec.name)
        return tuple(created)

    async def run_tick(self) -> ImportReport:
        """One read-map-plan-apply pass."""
        documents = await self._source.read()
        desired: list[ImportNode] = []
        covered: list[str] = []
        skipped: list[str] = []
        for document in documents:
            try:
                mapped = self._format.nodes_for(document)
            except ImportDocumentError as err:
                skipped.append(str(err))
                logger.warning("import: %s", err)
                continue
            desired.extend(mapped.nodes)
            covered.extend(mapped.covers)
        planned = imports.plan(
            desired, self._format.types, covered,
            self._repository.graph, self._repository.field_catalog(),
        )
        created, updated, failed = await self._apply(planned)
        return ImportReport(
            documents=len(documents), created=created, updated=updated,
            failed=tuple(failed), skipped=tuple(skipped),
        )

    async def _apply(self, planned: ImportPlan) -> tuple[int, int, list[str]]:
        summaries = {t.name: t.summary for t in self._format.types}
        created = updated = 0
        failed: list[str] = []
        for want in planned.creates:
            try:
                await self._repository.create_node(NodeDraft(
                    type=want.type, name=want.name,
                    summary=summaries.get(want.type, ""), fields=want.fields,
                ))
            except GraphContextError as err:
                failed.append(f"create {want.type} {want.name!r}: {err}")
                continue
            created += 1
        for change in planned.updates:
            try:
                await self._repository.update_node(change.node_id, fields=change.fields)
            except GraphContextError as err:
                failed.append(f"update {change.name!r}: {err}")
                continue
            updated += 1
        for message in failed:
            logger.warning("import: %s", message)
        return created, updated, failed


def describe_report(report: ImportReport) -> Sequence[str]:
    """Log lines for a pass that did something."""
    lines = []
    if report.wrote:
        lines.append(
            f"import: {report.created} created, {report.updated} updated "
            f"from {report.documents} document(s)"
        )
    lines.extend(f"import failure: {m}" for m in report.failed)
    return lines
