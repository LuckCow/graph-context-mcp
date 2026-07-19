"""Core domain entities and value objects.

All models here are immutable (frozen dataclasses). Updates are expressed
as ``dataclasses.replace`` producing new values; the only mutable state in
the domain lives inside :class:`graph_context.domain.graph.GraphIndex` and
the session objects, where mutability *is* the point.

``NodeDraft`` vs ``Node``: ids are assigned by the storage layer (Anytype
mints object ids), so use-cases build a draft and receive a ``Node`` back
from the repository. This keeps id-generation policy out of the domain.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from graph_context.domain import schema
from graph_context.domain.schema import Role
from graph_context.errors import SchemaViolation

NodeId = str
"""Opaque node identifier, owned by the storage layer."""


class Detail(StrEnum):
    """How much of a node reaches the LLM: name line, +summary, +body.

    Historically an interface-only rendering knob; since the working set
    holds nodes *at* a detail level (WP15) it is a persisted domain value.
    ``interface.presenters`` re-exports it for the rendering call sites.
    """

    NAMES = "names"
    SUMMARIES = "summaries"
    FULL = "full"

TimelineValue = float | str
"""A node's position on the Event timeline (ADR 015): any ORDERED value.

Fiction uses numbers (``gc_story_time``); an assistant profile names a
native date property, whose ISO-8601 strings order lexicographically. A
space uses ONE representation -- values are compared against ``as_of``
and must be mutually comparable."""


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """One reflectable scalar property, as guidance for writes (ADR 023).

    The property-catalog shape returned by ``GraphRepository.field_catalog``
    and rendered into the overview / unmatched-key errors so the LLM reuses
    existing properties (by ``name`` or ``key``) instead of inventing new
    field keys. ``key`` is the raw store key (``""`` for the in-memory
    backend); ``options`` carries select/multi_select option names when the
    backend knows them cheaply (may be empty even for selects).
    """

    name: str
    format: str
    key: str = ""
    options: tuple[str, ...] = ()

    def render_hint(self) -> str:
        """The one property-hint line format both backends render into
        ``UnknownFieldKey`` messages: ``Name (format: options)``."""
        if self.options:
            return f"{self.name} ({self.format}: {', '.join(self.options)})"
        return f"{self.name} ({self.format})"


@dataclass(frozen=True, slots=True)
class PropertyDraft:
    """One NEW property in a schema proposal (WP33, ADR 041; ADR 042).

    The LLM-drafted half of a user-confirmed schema change: a display
    ``name``, a :data:`graph_context.domain.schema.CREATABLE_FORMATS`
    format (``objects`` makes it a relation -- an edge label once
    attached), and (for selects) the option names to seed. Invariants are
    enforced at construction so a malformed draft can never reach a
    repository.
    """

    name: str
    format: str
    options: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise SchemaViolation("property 'name' must be a non-empty string")
        if self.name.strip().lower().startswith("gc_"):
            raise SchemaViolation(
                f"property name {self.name!r} uses the reserved gc_ prefix "
                "(infrastructure vocabulary); pick a human name"
            )
        if self.format.strip().lower() not in schema.CREATABLE_FORMATS:
            raise SchemaViolation(
                f"unknown format {self.format!r} for property {self.name!r}; "
                f"formats: {', '.join(sorted(schema.CREATABLE_FORMATS))}"
            )
        if self.options and self.format not in {"select", "multi_select"}:
            raise SchemaViolation(
                f"property {self.name!r} ({self.format}) cannot carry "
                "options; only select and multi_select do"
            )
        if any(not option.strip() for option in self.options):
            raise SchemaViolation(
                f"property {self.name!r} has an empty option name"
            )

    def render_hint(self) -> str:
        """Same one-line shape as :meth:`FieldSpec.render_hint`."""
        if self.options:
            return f"{self.name} ({self.format}: {', '.join(self.options)})"
        return f"{self.name} ({self.format})"


def validate_property_drafts(drafts: Sequence[PropertyDraft]) -> None:
    """Cross-draft invariant of one schema proposal: no duplicate names.

    (Per-draft invariants live in :class:`PropertyDraft` itself.)
    """
    seen: set[str] = set()
    for draft in drafts:
        lowered = draft.name.strip().lower()
        if lowered in seen:
            raise SchemaViolation(
                f"property {draft.name!r} appears twice in one proposal"
            )
        seen.add(lowered)


DECLARATION_SCOPES: tuple[str, str] = ("instance", "type")
"""Where a newly declared property applies (ADR 042).

``instance``: the property is minted space-level and its value written on
the one object -- attached to no type (today's ``create_missing_fields``
behaviour). ``type``: same immediate mint + value, PLUS the write drafts a
user-confirmed schema proposal attaching the property to the node's type
(the ADR 041 flow) -- required for automation rules to watch it.
"""


@dataclass(frozen=True, slots=True)
class PropertyDeclaration:
    """One entry of a write's ``create_missing_properties`` map (ADR 042).

    ``key`` is the ``properties``-dict key it licenses; ``format`` is a
    :data:`graph_context.domain.schema.CREATABLE_FORMATS` member
    (``objects`` mints a relation -- an edge label); ``scope`` is a
    :data:`DECLARATION_SCOPES` member; ``name`` optionally overrides the
    minted property's display name (else one is derived from ``key``).
    Construction-validated so a malformed declaration never reaches a
    repository; normalisation (format/scope lowering) happens here too so
    every consumer sees canonical values.
    """

    key: str
    format: str
    scope: str = "instance"
    name: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "format", self.format.strip().lower())
        object.__setattr__(self, "scope", self.scope.strip().lower())
        if not self.key.strip():
            raise SchemaViolation(
                "create_missing_properties has an entry with an empty key"
            )
        if self.key.strip().lower().startswith("gc_"):
            raise SchemaViolation(
                f"property key {self.key!r} uses the reserved gc_ prefix "
                "(infrastructure vocabulary); pick a human name"
            )
        if self.format not in schema.CREATABLE_FORMATS:
            raise SchemaViolation(
                f"unknown format {self.format!r} for new property "
                f"{self.key!r}; formats: "
                f"{', '.join(sorted(schema.CREATABLE_FORMATS))}"
            )
        if self.scope not in DECLARATION_SCOPES:
            raise SchemaViolation(
                f"unknown scope {self.scope!r} for new property {self.key!r}; "
                "scope is 'instance' (a fact about this one object) or "
                "'type' (an attribute every object of the type should carry; "
                "drafts a user-confirmed schema change)"
            )

    @property
    def display_name(self) -> str:
        """The minted property's human display name: the explicit ``name``
        override, else one derived from the key (``shift_active`` ->
        ``Shift Active``) so a snake_case tool key never becomes the label
        humans see in the editor."""
        if self.name.strip():
            return self.name.strip()
        cleaned = self.key.strip().replace("_", " ").replace("-", " ")
        words = cleaned.split()
        titled = " ".join(
            word if word[:1].isupper() else word.capitalize() for word in words
        )
        return titled or self.key.strip()


def validate_property_declarations(
    written_keys: Sequence[str] | frozenset[str] | set[str],
    declarations: Mapping[str, PropertyDeclaration],
) -> None:
    """Well-formedness of a write's new-property declarations (ADR 042).

    Every declared key must also carry a value in ``properties`` (a
    declaration without a value writes nothing). Per-declaration invariants
    live in :class:`PropertyDeclaration`; whether a key *needs* declaring
    -- i.e. whether it matches an existing property -- is the repository's
    call, not ours.
    """
    written = {str(key) for key in written_keys}
    for key in declarations:
        if key not in written:
            raise SchemaViolation(
                f"create_missing_properties declares {key!r} but "
                "'properties' carries no value for it; every declared key "
                "needs a value in 'properties'"
            )


@dataclass(frozen=True, slots=True)
class Edge:
    """A directed, labelled link between two nodes.

    ``type`` is the *cleaned display label* (e.g. ``"knows"``, ``"boss"``)
    used for filtering and rendering. ``property_key`` is the raw Anytype
    relation key the edge was read from / must be written back to (e.g.
    ``"gc_edge_knows"``, ``"triggered_by"``); ``""`` for synthetic edges or
    the in-memory backend. Both participate in identity so two genuinely
    different relations that clean to the same label stay distinct.
    """

    source: NodeId
    type: str
    target: NodeId
    property_key: str = ""


@dataclass(frozen=True, slots=True)
class LinkSpec:
    """One link requested as part of a composite write.

    ``edge_type`` is the requested relation *label*; the repository resolves
    it to an existing relation's property key (or surfaces it for approval).
    The edge always runs *from* the node being written *to* ``other`` -- an
    edge is an entry in an ``objects``-format property on its source (ADR
    003), so the reverse direction is the other node's own write (ADR 042
    retired ``outgoing=False``).
    """

    edge_type: str
    other: NodeId

    def to_edge(self, anchor: NodeId, property_key: str = "") -> Edge:
        """Materialise this spec relative to the node being written."""
        return Edge(
            source=anchor, type=self.edge_type, target=self.other,
            property_key=property_key,
        )


@dataclass(frozen=True, slots=True)
class NodeDraft:
    """Everything needed to create a node, minus the storage-assigned id.

    ``body`` is the node's long-form content (Markdown): its description
    on ordinary nodes, the rendered text on Prose nodes (ADR 010 unified
    the two). It is persisted to the store at creation but is deliberately
    **not** part of :class:`Node` and never enters the GraphIndex: bodies
    can be thousands of words and the store never returns them on
    list/search anyway (A7). Retrieval is on-demand via
    ``GraphRepository.fetch_body``; updates go through
    ``GraphRepository.update_node(body=...)``. Prose and intent bodies
    stay immutable *by policy* (provenance must not be editable), not by
    API limitation.
    """

    type: str
    name: str
    summary: str
    story_time: TimelineValue | None = None
    fields: Mapping[str, str] = field(default_factory=dict)
    body: str = ""
    # A single emoji shown on the page, in lists, and in the graph view.
    # Cosmetic and human-owned after creation: set once here, never
    # updated by the server (a human's recolor/re-pick always survives).
    icon: str = ""


@dataclass(frozen=True, slots=True)
class Node:
    """A persisted node. Identity is ``id``; everything else is data.

    ``summary_stale`` implements the proposal's summary lifecycle: any
    update that does not carry a fresh summary flips this to ``True``
    (rule lives in the ``NodeWriter`` use-case, not here).

    ``story_time`` is only meaningful for nodes whose ``role`` is
    ``Role.EVENT``; it is the node's position on the story timeline and drives
    ``as_of`` filtering. ``fields`` holds type-specific extras we have not
    promoted to first-class attributes yet.

    ``type`` is the Anytype type's *display name* (rendered to the user);
    ``type_key`` is its raw key (used for writes); ``role`` is the resolved
    semantic role (``None`` for types with no mapped role).
    """

    id: NodeId
    type: str
    name: str
    summary: str
    summary_stale: bool = False
    story_time: TimelineValue | None = None
    fields: Mapping[str, str] = field(default_factory=dict)
    type_key: str = ""
    role: Role | None = None
    # Store-clock change stamp (sortable ISO; "" only when the store has
    # not surfaced one). A ranking signal (ADR 016 recency weight) and the
    # rule engine's built-in watchable (ADR 042), never content. Both
    # backends stamp it: Anytype from last_modified_date/created_date, the
    # in-memory fake from its own deterministic clock.
    modified_at: str = ""
