"""External snapshot imports: desired state + the reconcile plan (ADR 062).

An external system (the RuneLite plugin, today) exports its state as
documents. This module owns the two pure halves of turning those into
graph nodes the rest of the system can react to:

* the **desired-state vocabulary** -- :class:`ImportType` declares a
  native type the import owns, :class:`ImportNode` one node's name and
  wire-string property values, :class:`ImportFormat` is the seam a
  source-specific mapper implements (``domain/runescape.py`` is the
  first);
* the **planner** -- :func:`plan` diffs desired nodes against the live
  :class:`~graph_context.domain.graph.GraphIndex` and yields the
  creates and CHANGE-ONLY updates that make the graph match. Nothing is
  ever deleted: a node the export stopped mentioning gets the type's
  ``absent_fields`` (an emptied inventory holding reads quantity 0), or
  is left alone when the type declares none.

The game is the ground truth (ADR 062): the plan is computed against
the INDEX, not against the previous export, so a value a human edited
in Anytype is written back on the next tick -- and because the import
listener runs before the rule listener on the same change tick, the
rule engine never observes the human's value at all. This module is
pure: no I/O, no clocks; the importer service applies the plan.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from graph_context.domain import fields as domain_fields
from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import FieldSpec, Node, NodeId, PropertyDraft
from graph_context.domain.schema import INFRA_ROLES
from graph_context.errors import GraphContextError


class ImportDocumentError(GraphContextError):
    """A document decoded but does not have the shape its kind promises.

    Raised by a mapper; the importer skips THAT document and logs the
    message, so one malformed export never blocks the others.
    """


@dataclass(frozen=True, slots=True)
class ImportDocument:
    """One decoded export document. ``kind`` is the source's own label
    for it (the file stem, for a directory source)."""

    kind: str
    payload: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ImportType:
    """A native type an import owns: its name, plural, scalar properties,
    and what to write onto an existing node the export no longer lists.

    ``absent_fields`` empty means "leave unmentioned nodes alone" -- the
    right default for a type whose members are stable (skills, quests).
    """

    name: str
    plural: str
    properties: tuple[PropertyDraft, ...]
    summary: str = ""
    absent_fields: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ImportNode:
    """One desired node: type + name identify it (case-insensitive, like
    ``GraphIndex.find_by_name``); ``fields`` maps property DISPLAY names
    to wire strings in the read-back form (see :func:`wire_value`)."""

    type: str
    name: str
    fields: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class DocumentNodes:
    """A mapper's reading of one document: the nodes it describes and
    the types it describes COMPLETELY (``covers``). Absence handling
    applies only to covered types, so a document that failed to decode
    this tick can never zero out the nodes it would have listed."""

    nodes: tuple[ImportNode, ...]
    covers: tuple[str, ...] = ()


class ImportFormat(Protocol):
    """A source-specific mapper: the types it owns and how one document
    becomes nodes. Pure by contract."""

    @property
    def types(self) -> Sequence[ImportType]: ...

    def nodes_for(self, document: ImportDocument) -> DocumentNodes:
        """Map one document; raise :class:`ImportDocumentError` when it
        does not have the shape its kind promises."""
        ...


def wire_value(fmt: str, value: Any) -> str:
    """A Python value as the string a ``fields`` entry reads back as --
    the one place import values take their wire shape, so the planner's
    change-only compare agrees with what both backends store."""
    if value is None:
        return ""
    if fmt == "checkbox":
        return "true" if value else "false"
    if fmt == "number":
        return domain_fields.render_number(float(value))
    return str(value)


@dataclass(frozen=True, slots=True)
class NodeUpdate:
    node_id: NodeId
    name: str
    fields: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ImportPlan:
    creates: tuple[ImportNode, ...]
    updates: tuple[NodeUpdate, ...]

    @property
    def empty(self) -> bool:
        return not self.creates and not self.updates


def plan(
    desired: Sequence[ImportNode],
    types: Sequence[ImportType],
    covered: Sequence[str],
    index: GraphIndex,
    catalog: Mapping[str, Sequence[FieldSpec]],
) -> ImportPlan:
    """The creates and change-only updates that make ``index`` match
    ``desired``.

    Matching is by (type, name), case-insensitive, among non-infra
    nodes; a duplicate pair resolves to the lowest id, deterministically.
    ``catalog`` (the repository's ``field_catalog``) maps display names
    to the keys ``Node.fields`` is stored under; a name the catalog does
    not know compares under itself (the open-vocabulary backend stores
    display names verbatim). Absent values compare as ``""``, or
    ``"false"`` for a checkbox -- the adapter drops both on read.
    """
    by_type = {t.name.casefold(): t for t in types}
    existing: dict[tuple[str, str], Node] = {}
    for node in sorted(index.nodes(), key=lambda n: n.id):
        if node.role in INFRA_ROLES or node.type.casefold() not in by_type:
            continue
        existing.setdefault((node.type.casefold(), node.name.casefold()), node)

    creates: list[ImportNode] = []
    updates: list[NodeUpdate] = []
    mentioned: set[tuple[str, str]] = set()
    for want in desired:
        pair = (want.type.casefold(), want.name.casefold())
        mentioned.add(pair)
        current = existing.get(pair)
        if current is None:
            creates.append(want)
            continue
        changed = _changed_fields(current, want.fields, by_type[pair[0]], catalog)
        if changed:
            updates.append(NodeUpdate(current.id, current.name, changed))

    covered_types = {name.casefold() for name in covered}
    for pair, held in existing.items():
        spec = by_type[pair[0]]
        if pair in mentioned or pair[0] not in covered_types or not spec.absent_fields:
            continue
        changed = _changed_fields(held, spec.absent_fields, spec, catalog)
        if changed:
            updates.append(NodeUpdate(held.id, held.name, changed))
    return ImportPlan(tuple(creates), tuple(updates))


def _changed_fields(
    node: Node,
    wanted: Mapping[str, str],
    spec: ImportType,
    catalog: Mapping[str, Sequence[FieldSpec]],
) -> dict[str, str]:
    formats = {p.name.casefold(): p.format for p in spec.properties}
    keys = _store_keys(spec.name, catalog)
    changed: dict[str, str] = {}
    for name, value in wanted.items():
        # The store key first (the adapter stores under raw keys), then
        # the display name (the open-vocabulary backend stores verbatim);
        # both through the shared read rule, like the rule engine.
        current = domain_fields.lookup(node.fields, keys.get(name.casefold(), name))
        if not current:
            current = domain_fields.lookup(node.fields, name)
        fmt = formats.get(name.casefold())
        if not current and fmt == "checkbox":
            current = "false"
        if fmt in {"select", "multi_select"}:
            # A select write resolves to the existing option under the
            # store's identity rule and reads back in ITS spelling.
            if domain_fields.same_option(current, value):
                continue
        elif current == value:
            continue
        changed[name] = value
    return changed


def _store_keys(
    type_name: str, catalog: Mapping[str, Sequence[FieldSpec]]
) -> dict[str, str]:
    """Display name (casefolded) -> the key ``Node.fields`` stores it under."""
    target = type_name.casefold()
    for listed, specs in catalog.items():
        if listed.casefold() == target:
            return {s.name.casefold(): (s.key or s.name) for s in specs}
    return {}
