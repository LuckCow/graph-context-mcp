"""Port: where an external system's export documents come from (ADR 062).

The importer service asks a source for the CURRENT set of decoded
documents every change tick and reconciles the graph against them; it
neither knows nor cares whether they are files in a directory (the
production adapter, ``infrastructure/imports``), an HTTP endpoint, or a
test's canned list. The contract is cheap-when-unchanged: an
implementation caches its decodes and re-reads only what moved, because
the tick that calls it runs every few seconds.
"""

from __future__ import annotations

from typing import Protocol

from graph_context.domain.imports import ImportDocument


class ImportSource(Protocol):
    """The current export documents, every call.

    A document that cannot be decoded right now (a file mid-write) is
    represented by its LAST good decode, or omitted if it never decoded
    -- never raised: a torn read must not stall the rest of the batch.
    Raises :class:`graph_context.errors.GraphContextError` only when the
    source itself is unreachable (the directory is gone).
    """

    async def read(self) -> tuple[ImportDocument, ...]: ...
