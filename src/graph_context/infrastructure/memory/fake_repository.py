"""In-memory reference implementation of :class:`GraphRepository`.

Two jobs:

1. **Tests and offline development.** Application services are exercised
   against this fake, so the whole use-case layer is testable without a
   running Anytype instance.
2. **Executable specification.** ``AnytypeGraphRepository`` must match this
   behaviour exactly (see ``tests/contract``) -- in particular the
   composite-create rollback contract -- with the only difference being
   write-through persistence to the Anytype API and id assignment by
   Anytype.

Ids here are sequential (``n0001``...) purely for readable test output.
The methods are ``async`` to satisfy the port, but never actually await.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from itertools import count
from typing import Any

from graph_context.domain import attribution, schema
from graph_context.domain import fields as domain_fields
from graph_context.domain.graph import GraphIndex
from graph_context.domain.models import (
    Edge,
    FieldSpec,
    LinkSpec,
    Node,
    NodeDraft,
    NodeId,
    PropertyDraft,
    TimelineValue,
)
from graph_context.domain.schema import Role
from graph_context.errors import (
    SchemaChangeConflict,
    UnknownFieldKey,
    UnknownNodeType,
    UnknownRelationLabel,
)


@dataclass(frozen=True)
class FakeTemplate:
    """A type's template, as the fake models its resolved effect: default field
    values applied on create, and a scaffold body prepended to the caller's."""

    default_fields: Mapping[str, str] = field(default_factory=dict)
    body: str = ""


class InMemoryGraphRepository:
    """``GraphRepository`` whose only store is its own :class:`GraphIndex`."""

    def __init__(
        self,
        *,
        role_overrides: Mapping[str, Role] | None = None,
        templates: Mapping[str, FakeTemplate] | None = None,
        field_catalog: Sequence[FieldSpec] | None = None,
        members: Sequence[str] = (),
    ) -> None:
        self._graph = GraphIndex()
        self._ids = count(1)
        self._bodies: dict[NodeId, str] = {}
        self._out_of_band: list[NodeDraft] = []
        # Profile-supplied type-key -> Role additions (WP5); same contract
        # as the Anytype adapter's registry overrides.
        self._role_overrides: dict[str, Role] = dict(role_overrides or {})
        # Type-identifier -> template, mirroring the Anytype adapter applying a
        # type's template on create (default field values + scaffold body).
        self._templates: dict[str, FakeTemplate] = dict(templates or {})
        # The space's property catalog (ADR 023). None keeps the
        # historical open behavior -- any field key or edge label, stored
        # verbatim -- so the memory backend and demos need no vocabulary.
        # A catalog turns on the strict contract: story-node field keys
        # must match a spec (by key or name) or be declared via
        # create_missing_fields, and link labels must match an ``objects``
        # spec (canonicalizing to its name) or be minted via
        # create_missing_relations. An ``objects``-format spec models a
        # RELATION (an edge, ADR 006): never a fields target, and a fields
        # key naming one redirects to ``links`` -- mirroring the adapter's
        # registry.
        self._field_specs: list[FieldSpec] | None = None
        if field_catalog is not None:
            self._adopt_catalog(field_catalog)
        # Types minted through the WP33 schema-change port surface:
        # display name -> the type's own property specs. The adapter keys
        # per-type properties off its registry; this is the fake's mirror.
        self._minted_types: dict[str, list[FieldSpec]] = {}
        # Space members reflected as read-only nodes (S11), mirroring the
        # Anytype adapter's member fetch: first-class, linkable (an
        # assignee-style edge needs a target IN the index), no role.
        self._reflect_members(members)

    def _adopt_catalog(self, field_catalog: Sequence[FieldSpec]) -> None:
        specs = self._field_specs or []
        specs.extend(field_catalog)
        # Bootstrap parity (ADR 028): the Anytype adapter's
        # ensure_schema guarantees the attribution properties exist,
        # so recorder writes resolve without an opt-in. The fake's
        # catalog carries the same guarantee.
        existing_keys = {spec.key for spec in specs}
        specs.extend(
            FieldSpec(name=key, format=fmt, key=key)
            for key, fmt in attribution.ATTRIBUTION_FIELDS.items()
            if key not in existing_keys
        )
        self._field_specs = specs

    def _reflect_members(self, members: Sequence[str]) -> None:
        for name in members:
            self._graph.upsert_node(Node(
                id=f"member-{next(self._ids):04d}",
                type="Space member",
                type_key="participant",
                name=name,
                summary="",
            ))

    @property
    def graph(self) -> GraphIndex:
        return self._graph

    async def create_node(
        self,
        draft: NodeDraft,
        links: Sequence[LinkSpec] = (),
        *,
        create_missing_relations: bool = False,
        create_missing_fields: Mapping[str, str] | None = None,
    ) -> Node:
        role = schema.resolve_role(draft.type, self._role_overrides)
        # Apply the type's template (default field values + scaffold body),
        # except for infra roles -- matching the Anytype adapter. Caller fields
        # override template defaults; the caller body is appended below the
        # scaffold (template first).
        template = None if role in schema.INFRA_ROLES else self._templates.get(draft.type)
        fields = self._resolve_fields(
            draft.fields, type_name=draft.type,
            create_missing=create_missing_fields,
        )
        body = draft.body
        if template is not None:
            fields = {**template.default_fields, **fields}
            body = (
                f"{template.body}\n{draft.body}"
                if draft.body and template.body
                else draft.body or template.body
            )
        node = Node(
            id=f"n{next(self._ids):04d}",
            # Display name mirrors the Anytype backend: a mapped role renders
            # as its role name (gc_prose -> "Prose"), else the raw identifier.
            type=role.value if role is not None else draft.type,
            name=draft.name,
            summary=draft.summary,
            story_time=draft.story_time,
            fields=fields,
            type_key=draft.type,
            role=role,
        )
        self._graph.upsert_node(node)
        if body:
            self._bodies[node.id] = body
        try:
            for link in links:
                label = self._resolve_link_label(
                    link.edge_type, create_missing_relations
                )
                self._graph.add_edge(
                    replace(link, edge_type=label).to_edge(anchor=node.id)
                )
        except Exception:
            # Composite-create contract: never leave a half-applied write.
            self._graph.remove_node(node.id)
            self._bodies.pop(node.id, None)
            raise
        return node

    async def update_node(
        self,
        node_id: NodeId,
        *,
        name: str | None = None,
        summary: str | None = None,
        summary_stale: bool | None = None,
        body: str | None = None,
        story_time: TimelineValue | None = None,
        fields: Mapping[str, str] | None = None,
        create_missing_fields: Mapping[str, str] | None = None,
    ) -> Node:
        existing = self._graph.node(node_id)
        if fields is not None:
            fields = self._resolve_fields(
                fields, type_name=existing.type,
                create_missing=create_missing_fields,
            )
        changes: dict[str, Any] = {
            key: value
            for key, value in {
                "name": name,
                "summary": summary,
                "summary_stale": summary_stale,
                "story_time": story_time,
                "fields": dict(fields) if fields is not None else None,
            }.items()
            if value is not None
        }
        updated = replace(existing, **changes)
        self._graph.upsert_node(updated)
        if body is not None:
            # A7 semantics: wholesale replace; empty string clears.
            self._bodies[node_id] = body
        return updated

    async def add_link(
        self, anchor: NodeId, link: LinkSpec, *, create_missing_relations: bool = False
    ) -> Edge:
        label = self._resolve_link_label(link.edge_type, create_missing_relations)
        edge = replace(link, edge_type=label).to_edge(anchor=anchor)
        self._graph.add_edge(edge)
        return edge

    async def remove_link(self, edge: Edge) -> None:
        self._graph.remove_edge(edge)

    def role_for(self, type_identifier: str) -> Role | None:
        return schema.resolve_role(type_identifier, self._role_overrides)

    def known_node_types(self) -> frozenset[str]:
        # The in-memory backend has an open vocabulary; surface the mapped
        # (non-infra) roles as helpful create_node suggestions, plus any
        # types minted through the WP33 schema-change surface.
        return frozenset(
            r.value for r in Role if r not in schema.INFRA_ROLES
        ) | frozenset(self._minted_types)

    # -- schema changes (WP33, ADR 041) ----------------------------------

    async def create_type(
        self,
        name: str,
        *,
        plural: str = "",
        properties: Sequence[PropertyDraft] = (),
    ) -> str:
        display = name.strip()
        taken = {t.lower() for t in self.known_node_types()}
        if display.lower() in taken:
            raise SchemaChangeConflict(
                f"a type matching {display!r} already exists in this "
                "space; propose new properties on it instead"
            )
        self._minted_types[display] = [
            self._adopt_property_draft(draft) for draft in properties
        ]
        return display

    async def add_type_properties(
        self, type_identifier: str, properties: Sequence[PropertyDraft]
    ) -> str:
        display = self._existing_type_name(type_identifier)
        specs = self._minted_types.setdefault(display, [])
        for draft in properties:
            target = draft.name.strip().lower()
            held = next(
                (s for s in specs if s.name.strip().lower() == target), None
            )
            if held is not None:
                # Same on-type semantics as the adapter: a matching format
                # is an idempotent no-op, a mismatch stops the change (A12).
                if held.format != draft.format:
                    raise SchemaChangeConflict(
                        f"{display} already has a property named "
                        f"{draft.name!r} with format {held.format!r}, not "
                        f"{draft.format!r}; formats are immutable (A12) -- "
                        "reuse it as-is or pick another name"
                    )
                continue
            specs.append(self._adopt_property_draft(draft))
        return display

    def _existing_type_name(self, identifier: str) -> str:
        target = identifier.strip().lower()
        for name in self.known_node_types():
            if name.lower() == target:
                return name
        role = schema.resolve_role(identifier, self._role_overrides)
        if role is not None and role not in schema.INFRA_ROLES:
            return role.value
        raise UnknownNodeType(identifier, tuple(sorted(self.known_node_types())))

    def _adopt_property_draft(self, draft: PropertyDraft) -> FieldSpec:
        """The fake's half of the adapter's ``_property_entry`` contract:
        reuse an existing same-format property, conflict on a format
        mismatch (A12) or a relation name (ADR 006), else mint -- joining
        the catalog in catalog mode so writes resolve it thereafter."""
        if self._relation_spec_for(draft.name) is not None:
            raise SchemaChangeConflict(
                f"{draft.name!r} names an existing relation -- an edge, not "
                "a scalar property; pick a different name"
            )
        existing = (
            self._spec_for(draft.name) if self._field_specs is not None else None
        )
        if existing is not None:
            if existing.format != draft.format:
                raise SchemaChangeConflict(
                    f"a property named {existing.name!r} already exists in "
                    f"this space with format {existing.format!r}, not "
                    f"{draft.format!r}; formats are immutable (A12) -- "
                    "reuse it as-is or pick another name"
                )
            return existing
        spec = FieldSpec(
            name=draft.name, format=draft.format,
            key=draft.name.strip().lower().replace(" ", "_"),
            options=draft.options,
        )
        if self._field_specs is not None:
            self._field_specs.append(spec)
        return spec

    def known_edge_labels(self) -> frozenset[str]:
        if self._field_specs is None:
            # Open mode: no predefined relation vocabulary off a live space.
            return frozenset()
        return frozenset(
            spec.name for spec in self._field_specs if spec.format == "objects"
        )

    def _resolve_link_label(self, label: str, create_missing: bool) -> str:
        """The fake's half of the adapter's ``_resolve_relation`` contract.

        Open mode (no catalog): any label lands verbatim, as ever. Catalog
        mode: a link's ``edge_type`` must match an existing ``objects``
        relation (by key or display name, case-insensitive) and canonicalizes
        to that relation's label -- an unmatched label is surfaced for
        approval unless ``create_missing`` mints a new relation, which then
        joins the vocabulary for reuse.
        """
        if self._field_specs is None:
            return label
        spec = self._relation_spec_for(label)
        if spec is not None:
            return spec.name
        if not create_missing:
            raise UnknownRelationLabel(label, tuple(self.known_edge_labels()))
        minted = FieldSpec(
            name=label, format="objects",
            key=label.strip().lower().replace(" ", "_"),
        )
        self._field_specs.append(minted)
        return minted.name

    def relation_label_for(self, field_key: str) -> str | None:
        spec = self._relation_spec_for(field_key)
        return None if spec is None else spec.name

    def _relation_spec_for(self, label: str) -> FieldSpec | None:
        """Objects relations only, by key or display name -- the fake's
        ``key_for_label``. Open mode has no relation vocabulary: None."""
        if self._field_specs is None:
            return None
        target = label.strip().lower()
        for spec in self._field_specs:
            if spec.format == "objects" and target in (
                spec.key.strip().lower(), spec.name.strip().lower()
            ):
                return spec
        return None

    def field_catalog(self) -> Mapping[str, tuple[FieldSpec, ...]]:
        if not self._field_specs:
            # Open mode: only WP33-minted types carry a catalog (their
            # own properties); everything else stays vocabulary-free.
            return {
                name: tuple(s for s in own if s.format != "objects")
                for name, own in self._minted_types.items()
                if own
            }
        # No per-type property attachment in the fake: the whole catalog is
        # offered under every known (non-infra) type name -- WP33-minted
        # properties joined ``_field_specs`` at creation, so they are in
        # here too. Relations (objects format) are edges, not fields-key
        # vocabulary; the attribution stamps are recorder-owned (ADR 028),
        # not offered.
        specs = tuple(
            s for s in self._field_specs
            if s.format != "objects" and s.key not in attribution.ATTRIBUTION_FIELDS
        )
        return {name: specs for name in sorted(self.known_node_types())}

    # -- field routing (ADR 023) -------------------------------------------

    def _resolve_fields(
        self,
        fields: Mapping[str, str],
        *,
        type_name: str,
        create_missing: Mapping[str, str] | None,
    ) -> dict[str, str]:
        """The fake's half of the ADR 023/028 contract.

        Open mode (no catalog): fields pass through verbatim. Catalog
        mode (every role -- infra writes are native-only too, ADR 028):
        each key must match a spec by key or display name
        (case-insensitive) and is stored under the spec's canonical key --
        mirroring the adapter, where a display-name write reads back under
        the raw property key -- or be declared in ``create_missing``, which
        registers a new spec. Values normalize like the adapter round-trip
        (checkbox -> "true"/"false", numbers untrailed, multi_select
        comma-spacing).
        """
        if self._field_specs is None:
            return dict(fields)
        declared = {k: v.strip().lower() for k, v in (create_missing or {}).items()}
        # All-keys-first check: an approval error never half-extends the
        # catalog (same discipline as the adapter).
        matched: dict[str, FieldSpec | None] = {}
        for key in fields:
            spec = self._spec_for(key)
            if spec is not None and spec.format == "objects":
                # The key names a relation: an edge, never a field --
                # redirect (even when declared; a scalar must not shadow it).
                raise UnknownFieldKey(
                    key, type_name, relation_label=spec.name,
                )
            if spec is None and key not in declared:
                raise UnknownFieldKey(
                    key,
                    type_name,
                    type_properties=tuple(
                        s.render_hint()
                        for s in self._field_specs
                        if s.format != "objects"
                    ),
                    formats=tuple(schema.FIELD_FORMATS),
                )
            matched[key] = spec
        resolved: dict[str, str] = {}
        for key, value in fields.items():
            spec = matched[key]
            if spec is None:
                spec = FieldSpec(name=key, format=declared[key], key=key)
                self._field_specs.append(spec)
            store_key = spec.key or spec.name
            resolved[store_key] = self._normalize_value(spec, value)
        return resolved

    def _spec_for(self, key: str) -> FieldSpec | None:
        target = key.strip().lower()
        assert self._field_specs is not None
        for spec in self._field_specs:
            if target in (spec.key.strip().lower(), spec.name.strip().lower()):
                return spec
        return None

    def _normalize_value(self, spec: FieldSpec, value: str) -> str:
        """Match what the adapter reads back after a write (ADR 012's
        ``field_value`` normalization), so round-trips agree across repos.
        Acceptance rules and errors are the shared ``domain.fields`` ones."""
        field = spec.key or spec.name
        if spec.format == "checkbox":
            return "true" if domain_fields.parse_checkbox(field, value) else "false"
        if spec.format == "number":
            return domain_fields.render_number(
                domain_fields.parse_number(field, value)
            )
        if spec.format in {"select", "multi_select"}:
            names = domain_fields.split_multi_select(value)
            self._register_options(spec, names)
            return ", ".join(names) if spec.format == "multi_select" else value.strip()
        return value

    def _register_options(self, spec: FieldSpec, names: list[str]) -> None:
        """Unseen select values become options (the adapter auto-creates
        tags, ADR 012); recorded so error hints can list them."""
        assert self._field_specs is not None
        known = {opt.strip().lower() for opt in spec.options}
        new = [n for n in names if n.strip().lower() not in known]
        if new:
            updated = replace(spec, options=(*spec.options, *new))
            self._field_specs[self._field_specs.index(spec)] = updated

    def stage_out_of_band(self, draft: NodeDraft) -> None:
        """Queue a node that exists in the space but not the index yet.

        Simulates a human creating an object in the Anytype UI while the
        server runs: invisible to every read until :meth:`resync` pulls it
        in -- the same contract as the real adapter's modified-since
        fetch. Test/eval surface only; not part of the port.
        """
        self._out_of_band.append(draft)

    def stage_space_vocabulary(
        self,
        field_catalog: Sequence[FieldSpec] = (),
        members: Sequence[str] = (),
    ) -> None:
        """Adopt a space's property catalog and reflected members after
        construction -- same semantics as the constructor arguments of the
        same names. Any catalog (even one staged empty) switches fields
        resolution to the strict contract. Test/eval surface only, like
        :meth:`stage_out_of_band`: production composition passes the
        vocabulary at construction; only the eval fixture stages a
        case-specific space into an already-built runtime."""
        self._adopt_catalog(field_catalog)
        self._reflect_members(members)

    async def hydrate(self) -> None:
        """No backing store: the index is already authoritative here."""

    async def resync(self) -> frozenset[NodeId]:
        """Materialize whatever was staged out-of-band (usually nothing)."""
        staged, self._out_of_band = self._out_of_band, []
        created = [await self.create_node(draft) for draft in staged]
        return frozenset(node.id for node in created)

    async def fetch_body(self, node_id: NodeId) -> str:
        self._graph.node(node_id)  # NodeNotFound on bad id
        return self._bodies.get(node_id, "")
