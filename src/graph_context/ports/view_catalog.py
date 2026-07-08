"""ViewCatalog port: the user's saved Set views, compiled and runnable.

WP13's final phase (ADR 018): an Anytype Set view is a saved query the
human maintains in the desktop UI. Implementations COMPILE each view's
machine-readable definition (filters + sorts, spike S9) into a
:class:`~graph_context.domain.query.NodeQuery`, so views execute on the
same in-memory engine as every other read -- the store is a
view-definition source, never a second query engine.

Shaped like ``ModeStore``: one async ``load()`` returning everything,
called per use so a view edited in the desktop applies on the next
query. A view that cannot be compiled (unknown condition, no inferable
source type) is skipped by the implementation, not surfaced as an error
-- the catalog lists what the LLM can actually run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from graph_context.domain.query import NodeQuery


@dataclass(frozen=True, slots=True)
class SavedView:
    """One runnable view: names for lookup/rendering, the compiled query.

    ``query.limit``/``exclude_roles`` carry engine defaults; the tool
    layer overrides them per call (``dataclasses.replace``).
    """

    set_name: str
    view_name: str
    query: NodeQuery
    set_id: str = ""
    view_id: str = ""

    @property
    def full_name(self) -> str:
        return f"{self.set_name}/{self.view_name}"


class ViewCatalog(Protocol):
    """Read access to the space's compilable saved views."""

    async def load(self) -> tuple[SavedView, ...]:
        """Every compilable view, freshly read from the store."""
        ...
