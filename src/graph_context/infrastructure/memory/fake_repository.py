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
from typing import Any, NoReturn

from graph_context.domain import activity, attribution, revisions, schema
from graph_context.domain import fields as domain_fields
from graph_context.domain.graph import Direction, GraphIndex
from graph_context.domain.models import (
    Edge,
    FieldSpec,
    LinkSpec,
    Node,
    NodeDraft,
    NodeId,
    PropertyDeclaration,
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
        attachments: Mapping[str, Sequence[str]] | None = None,
        members: Sequence[str] = (),
    ) -> None:
        self._graph = GraphIndex()
        self._ids = count(1)
        # Deterministic store clock: every write stamps Node.modified_at
        # with a strictly increasing sortable ISO string, mirroring the
        # Anytype backend's last_modified_date (the rule engine's built-in
        # watchable, ADR 042, needs the fake to tick too).
        self._clock = count(1)
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
        # must match a spec (by key or name) or carry a scalar
        # ``create_missing`` declaration, and link labels must match an
        # ``objects`` spec (canonicalizing to its name) or be minted via
        # an ``objects`` declaration (ADR 042). An ``objects``-format spec
        # models a RELATION (an edge, ADR 006): never a fields target, and
        # a fields key naming one redirects to the relation surface --
        # mirroring the adapter's registry.
        self._field_specs: list[FieldSpec] | None = None
        if field_catalog is not None:
            self._adopt_catalog(field_catalog)
        # Per-type property attachment: display name -> the type's own
        # property specs. Holds both configured ``attachments`` and types
        # minted through the WP33 schema-change port surface (whose
        # properties attach on apply, like the adapter registering the
        # type). The adapter keys per-type properties off its registry;
        # this is the fake's mirror.
        self._type_props: dict[str, list[FieldSpec]] = {}
        # ADR 047 switch: an ``attachments`` mapping (even an empty one)
        # turns on type-scoped bare resolution -- catalog keys resolve
        # bare only on types that carry them (plus the instance/infra/
        # gc_edge exemptions). Without it a catalog keeps the historical
        # flat behavior (every property usable on every type), so
        # fixtures opt into scoping explicitly.
        self._scoped = attachments is not None
        if attachments is not None:
            self._adopt_attachments(attachments)
        # Space members reflected as read-only nodes (S11), mirroring the
        # Anytype adapter's member fetch: first-class, linkable (an
        # assignee-style edge needs a target IN the index), no role.
        self._reflect_members(members)

    def _adopt_attachments(
        self, attachments: Mapping[str, Sequence[str]]
    ) -> None:
        if self._field_specs is None:
            raise ValueError(
                "attachments need a field_catalog: attachment entries are "
                "resolved against the space's property specs"
            )
        for type_name, identifiers in attachments.items():
            specs = self._type_props.setdefault(type_name, [])
            for identifier in identifiers:
                spec = self._spec_for(identifier)
                if spec is None:  # fixtures must fail loudly, not skew tests
                    raise ValueError(
                        f"attachments: no catalog property matches "
                        f"{identifier!r} (type {type_name!r})"
                    )
                if not any(self._same_spec(spec, held) for held in specs):
                    specs.append(spec)

    @staticmethod
    def _same_spec(a: FieldSpec, b: FieldSpec) -> bool:
        """Identity for attachment membership: the canonical store key
        (options updates swap spec instances, so object identity lies)."""
        return (a.key or a.name).strip().lower() == (b.key or b.name).strip().lower()

    def _own_specs(self, type_name: str) -> list[FieldSpec]:
        target = type_name.strip().lower()
        for name, specs in self._type_props.items():
            if name.strip().lower() == target:
                return specs
        return []

    def _admits(
        self, type_name: str, spec: FieldSpec, instance: Node | None
    ) -> bool:
        """The fake's half of the ADR 047 bare-resolution scope: the
        type's attached specs, plus the object's own vocabulary on
        update. Exempt (space-wide): infra-role targets, the seeded
        ``gc_edge_*`` starter relations, and the attribution stamps
        (recorders write those onto ANY type -- a capture's artifact
        type is native under ADR 015) -- mirroring the adapter."""
        if not self._scoped:
            return True
        role = schema.resolve_role(type_name, self._role_overrides)
        if role in schema.INFRA_ROLES:
            return True
        if spec.key.startswith("gc_edge_"):
            return True
        if spec.key in attribution.ATTRIBUTION_FIELDS:
            return True
        if any(self._same_spec(spec, held) for held in self._own_specs(type_name)):
            return True
        if instance is not None:
            if spec.format == "objects":
                return any(
                    edge.type == spec.name
                    for edge in self._graph.edges(instance.id, Direction.OUT)
                )
            return (spec.key or spec.name) in instance.fields
        return False

    def _adopt_catalog(self, field_catalog: Sequence[FieldSpec]) -> None:
        specs = self._field_specs or []
        specs.extend(field_catalog)
        # Bootstrap parity (ADR 028): the Anytype adapter's
        # ensure_schema guarantees the attribution properties exist,
        # so recorder writes resolve without an opt-in. The fake's
        # catalog carries the same guarantee -- and since ADR 045 the
        # same holds for the mode-config surface a meta-inspection
        # mode writes.
        existing_keys = {spec.key for spec in specs}
        specs.extend(
            FieldSpec(name=key, format=fmt, key=key)
            for key, fmt in (
                attribution.ATTRIBUTION_FIELDS | activity.MODE_CONFIG_FIELDS
                # ADR 049: the historian's surfaces, bootstrap-guaranteed
                # like the recorders' stamps.
                | revisions.HISTORY_FIELDS | revisions.TRACKED_TYPES_FIELDS
            ).items()
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

    def _tick(self) -> str:
        """The next store-clock stamp: sortable ISO, strictly increasing."""
        return f"2026-01-01T00:00:00.{next(self._clock):06d}+00:00"

    async def create_node(
        self,
        draft: NodeDraft,
        links: Sequence[LinkSpec] = (),
        *,
        create_missing: Mapping[str, PropertyDeclaration] | None = None,
    ) -> Node:
        role = schema.resolve_role(draft.type, self._role_overrides)
        # Apply the type's template (default field values + scaffold body),
        # except for infra roles -- matching the Anytype adapter. Caller fields
        # override template defaults; the caller body is appended below the
        # scaffold (template first).
        template = None if role in schema.INFRA_ROLES else self._templates.get(draft.type)
        fields = self._resolve_fields(
            draft.fields, type_name=draft.type, create_missing=create_missing,
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
            modified_at=self._tick(),
        )
        self._graph.upsert_node(node)
        if body:
            self._bodies[node.id] = body
        try:
            for link in links:
                label = self._resolve_link_label(
                    link.edge_type,
                    self._link_declaration(link.edge_type, create_missing),
                    type_name=draft.type,
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

    @staticmethod
    def _link_declaration(
        label: str, declared: Mapping[str, PropertyDeclaration] | None
    ) -> PropertyDeclaration | None:
        """The declaration licensing this link label, if any -- only
        ``objects``-format declarations apply (the adapter's rule)."""
        target = label.strip().lower()
        for key, declaration in (declared or {}).items():
            if key.strip().lower() == target and declaration.format == "objects":
                return declaration
        return None

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
        create_missing: Mapping[str, PropertyDeclaration] | None = None,
    ) -> Node:
        existing = self._graph.node(node_id)
        if fields is not None:
            fields = self._resolve_fields(
                fields, type_name=existing.type, create_missing=create_missing,
                instance=existing,
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
        updated = replace(existing, **changes, modified_at=self._tick())
        self._graph.upsert_node(updated)
        if body is not None:
            # A7 semantics: wholesale replace; empty string clears.
            self._bodies[node_id] = body
        return updated

    async def add_link(
        self,
        anchor: NodeId,
        link: LinkSpec,
        *,
        create_missing: PropertyDeclaration | None = None,
    ) -> Edge:
        source = self._graph.node(anchor)
        label = self._resolve_link_label(
            link.edge_type, create_missing,
            type_name=source.type, instance=source,
        )
        edge = replace(link, edge_type=label).to_edge(anchor=anchor)
        self._graph.add_edge(edge)
        # Link writes bump the source's store clock, like the adapter's
        # PATCH does on the live server (ADR 042 built-in watchable).
        self._graph.upsert_node(replace(source, modified_at=self._tick()))
        return edge

    async def remove_link(self, edge: Edge) -> None:
        self._graph.remove_edge(edge)
        source = self._graph.node(edge.source)
        self._graph.upsert_node(replace(source, modified_at=self._tick()))

    def role_for(self, type_identifier: str) -> Role | None:
        return schema.resolve_role(type_identifier, self._role_overrides)

    def known_node_types(
        self, include_roles: frozenset[Role] = frozenset()
    ) -> frozenset[str]:
        # The in-memory backend has an open vocabulary; surface the mapped
        # (non-infra) roles as helpful create_node suggestions, plus any
        # types minted through the WP33 schema-change surface. Infra roles
        # join only via the caller's meta privilege (ADR 045).
        return frozenset(
            r.value for r in Role
            if r not in schema.INFRA_ROLES or r in include_roles
        ) | frozenset(self._type_props)

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
        self._type_props[display] = [
            self._adopt_property_draft(draft) for draft in properties
        ]
        return display

    async def add_type_properties(
        self, type_identifier: str, properties: Sequence[PropertyDraft]
    ) -> str:
        display = self._existing_type_name(type_identifier)
        specs = self._type_props.setdefault(display, [])
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
        mismatch (A12) -- a *scalar* draft naming a relation included (no
        scalar shadow of an edge, ADR 006); an ``objects`` draft naming an
        existing relation attaches it (ADR 042) -- else mint, joining the
        catalog in catalog mode so writes resolve it thereafter."""
        relation = self._relation_spec_for(draft.name)
        if relation is not None:
            if draft.format != "objects":
                raise SchemaChangeConflict(
                    f"{draft.name!r} names an existing relation -- an edge, "
                    f"not a {draft.format!r} property; formats are immutable "
                    "(A12), pick a different name"
                )
            return relation
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

    def _resolve_link_label(
        self,
        label: str,
        create_missing: PropertyDeclaration | None,
        *,
        type_name: str = "",
        instance: Node | None = None,
    ) -> str:
        """The fake's half of the adapter's ``_resolve_relation`` contract.

        Open mode (no catalog): any label lands verbatim, as ever. Catalog
        mode: a link's ``edge_type`` must match an existing ``objects``
        relation (by key or display name, case-insensitive) the write's
        type scope admits (ADR 047) and canonicalizes to that relation's
        label -- an unmatched label is surfaced for approval unless a
        declaration (ADR 042) widens resolution to the whole space
        (reusing an existing relation -- attach, never a twin) or mints a
        new one, which then joins the vocabulary for reuse. Edge labels
        derive from the requested label, like the adapter's (whose labels
        clean from the minted KEY); the declaration's display name is
        store cosmetics the fake has no surface for.
        """
        if self._field_specs is None:
            return label
        spec = self._relation_spec_for(label)
        if spec is not None and self._admits(type_name, spec, instance):
            return spec.name
        if create_missing is None:
            if not self._scoped:
                raise UnknownRelationLabel(label, tuple(self.known_edge_labels()))
            attached = tuple(
                s.name for s in self._own_specs(type_name)
                if s.format == "objects"
            )
            raise UnknownRelationLabel(
                label,
                tuple(self.known_edge_labels()),
                type_name=type_name,
                attached=attached,
                unattached=tuple(
                    self.known_edge_labels() - frozenset(attached)
                ),
            )
        if spec is not None:
            return spec.name  # declared reuse: attach, never mint a twin
        minted = FieldSpec(
            name=label, format="objects",
            key=label.strip().lower().replace(" ", "_"),
        )
        self._field_specs.append(minted)
        return minted.name

    def relation_label_for(
        self,
        field_key: str,
        *,
        on_type: str | None = None,
        on_node: NodeId | None = None,
    ) -> str | None:
        if (on_type is None) == (on_node is None):
            raise ValueError(
                "relation_label_for needs exactly one of on_type/on_node"
            )
        if schema.is_read_only_relation(field_key):
            # Store-owned on write even though it reads as edges: never
            # offered as a writable label (contract parity with the Anytype
            # adapter, where the real store 400s on such a write).
            return None
        spec = self._relation_spec_for(field_key)
        if spec is None:
            return None
        if on_node is not None:
            node = self._graph.node(on_node)  # NodeNotFound propagates
            return spec.name if self._admits(node.type, spec, node) else None
        assert on_type is not None
        return spec.name if self._admits(on_type, spec, None) else None

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

    def field_catalog(
        self, include_roles: frozenset[Role] = frozenset()
    ) -> Mapping[str, tuple[FieldSpec, ...]]:
        if not self._field_specs:
            # Open mode: only WP33-minted types carry a catalog (their
            # own properties); everything else stays vocabulary-free.
            return {
                name: tuple(s for s in own if s.format != "objects")
                for name, own in self._type_props.items()
                if own
            }
        # The attribution stamps are recorder-owned (ADR 028) and the
        # mode-config keys (ADR 045) belong to the mode surface -- neither
        # is offered as generic story-write vocabulary. Relations (objects
        # format) are edges, not fields-key vocabulary.
        offerable = tuple(
            s for s in self._field_specs
            if s.format != "objects"
            and s.key not in attribution.ATTRIBUTION_FIELDS
            and s.key not in activity.MODE_CONFIG_FIELDS
            and s.key not in revisions.HISTORY_FIELDS
            and s.key not in revisions.TRACKED_TYPES_FIELDS
        )
        if not self._scoped:
            # Flat mode: the whole catalog offered under every known
            # (non-infra) type name -- the historical shape.
            return {
                name: offerable
                for name in sorted(self.known_node_types(include_roles))
            }
        # Scoped mode (ADR 047): per-type buckets from the attachments,
        # plus the adapter's "(any type)" bucket for unattached
        # properties -- discoverable, attachable via declaration, not
        # bare-usable.
        catalog: dict[str, tuple[FieldSpec, ...]] = {}
        claimed: set[str] = set()
        for name, own in self._type_props.items():
            role = schema.resolve_role(name, self._role_overrides)
            if role in schema.INFRA_ROLES and role not in include_roles:
                continue
            specs = tuple(s for s in own if s.format != "objects")
            claimed.update((s.key or s.name).strip().lower() for s in specs)
            if specs:
                catalog[name] = specs
        unclaimed = tuple(
            s for s in offerable
            if (s.key or s.name).strip().lower() not in claimed
            and not s.key.startswith("gc_")
        )
        if unclaimed:
            catalog["(any type)"] = unclaimed
        return catalog

    # -- field routing (ADR 023) -------------------------------------------

    def _resolve_fields(
        self,
        fields: Mapping[str, str],
        *,
        type_name: str,
        create_missing: Mapping[str, PropertyDeclaration] | None,
        instance: Node | None = None,
    ) -> dict[str, str]:
        """The fake's half of the ADR 023/028/042/047 contract.

        Open mode (no catalog): fields pass through verbatim. Catalog
        mode (every role -- infra writes are native-only too, ADR 028):
        each bare key must match a spec by key or display name
        (case-insensitive) that the write's type scope admits (ADR 047:
        the type's attached specs plus the object's own on update) and is
        stored under the spec's canonical key -- mirroring the adapter,
        where a display-name write reads back under the raw property key
        -- or carry a *scalar* declaration in ``create_missing``, which
        widens resolution to the whole space: an existing same-format
        spec is reused (this write attaches it), a format mismatch
        conflicts loudly (A12, the adapter's D7 rule), and a key matching
        nothing registers a new spec. A key naming a relation never
        resolves as a scalar, declared or not (ADR 006). Values normalize
        like the adapter round-trip (checkbox -> "true"/"false", numbers
        untrailed, multi_select comma-spacing).
        """
        if self._field_specs is None:
            return dict(fields)
        declared = {
            k: d for k, d in (create_missing or {}).items()
            if d.format != "objects"  # link labels resolve as relations
        }
        # All-keys-first check: an approval error never half-extends the
        # catalog (same discipline as the adapter).
        matched: dict[str, FieldSpec | None] = {}
        for key in fields:
            spec = self._spec_for(key)
            admitted = spec is not None and self._admits(
                type_name, spec, instance
            )
            if spec is not None and spec.format == "objects":
                if admitted:
                    # The key names a relation: an edge, never a field --
                    # redirect (even when declared; a scalar must not
                    # shadow it).
                    raise UnknownFieldKey(
                        key, type_name, relation_label=spec.name,
                    )
                # An unattached relation is never a scalar target either,
                # declared or not -- the sectioned error teaches the
                # objects-format attach gesture.
                self._raise_unknown_field(key, type_name, spec)
            if spec is not None and not admitted and key not in declared:
                self._raise_unknown_field(key, type_name, spec)
            if spec is None and key not in declared:
                self._raise_unknown_field(key, type_name, None)
            declaration = declared.get(key)
            if (
                spec is not None
                and declaration is not None
                and spec.format != declaration.format
            ):
                raise SchemaChangeConflict(
                    f"a property named {spec.name!r} already exists in this "
                    f"space with format {spec.format!r}, not "
                    f"{declaration.format!r}; formats are immutable (A12) -- "
                    "reuse it as-is (drop the declaration) or pick another "
                    "key"
                )
            matched[key] = spec
        resolved: dict[str, str] = {}
        for key, value in fields.items():
            spec = matched[key]
            if spec is None:
                declaration = declared[key]
                spec = FieldSpec(
                    name=declaration.display_name,
                    format=declaration.format,
                    key=key,
                )
                self._field_specs.append(spec)
            store_key = spec.key or spec.name
            resolved[store_key] = self._normalize_value(spec, value)
        return resolved

    def _raise_unknown_field(
        self, key: str, type_name: str, space_match: FieldSpec | None
    ) -> NoReturn:
        """The unadmitted-key approval error, mirroring the adapter's
        ``_raise_unknown_field``: flat mode keeps the historical
        space-wide listing; scoped mode renders the ADR 047 sections
        (the type's own vocabulary first, then unattached space
        vocabulary with the attach gesture, with the exact space match
        named when there is one)."""
        assert self._field_specs is not None
        if not self._scoped:
            raise UnknownFieldKey(
                key,
                type_name,
                type_properties=tuple(
                    s.render_hint()
                    for s in self._field_specs
                    if s.format != "objects"
                ),
                formats=tuple(schema.CREATABLE_FORMATS),
            )
        own = self._own_specs(type_name)
        # Reflected gc_ surfaces (attribution/mode-config) belong to
        # dedicated writers, not the vocabulary this error teaches --
        # the adapter's GC_PREFIX exclusion.
        hidden = (
            attribution.ATTRIBUTION_FIELDS.keys()
            | activity.MODE_CONFIG_FIELDS.keys()
        )
        unattached = [
            s for s in self._field_specs
            if not any(self._same_spec(s, held) for held in own)
            and s.key not in hidden
            and not s.key.startswith("gc_")
        ]
        raise UnknownFieldKey(
            key,
            type_name,
            type_properties=tuple(
                s.render_hint() for s in own if s.format != "objects"
            ),
            type_relations=tuple(
                s.name for s in own if s.format == "objects"
            ),
            unattached_properties=tuple(
                s.render_hint() for s in unattached if s.format != "objects"
            ),
            unattached_relations=tuple(
                s.name for s in unattached if s.format == "objects"
            ),
            formats=tuple(schema.CREATABLE_FORMATS),
            space_match_name=space_match.name if space_match else "",
            space_match_format=space_match.format if space_match else "",
        )

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
        if spec.format == "date":
            # Live reads every date back as the UTC instant (bare date ->
            # midnight UTC); naive timestamps error (R2).
            return domain_fields.normalize_date(field, value)
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
        attachments: Mapping[str, Sequence[str]] | None = None,
    ) -> None:
        """Adopt a space's property catalog and reflected members after
        construction -- same semantics as the constructor arguments of the
        same names. Any catalog (even one staged empty) switches fields
        resolution to the strict contract; an ``attachments`` mapping
        additionally turns on ADR 047 type-scoped bare resolution.
        Test/eval surface only, like :meth:`stage_out_of_band`:
        production composition passes the vocabulary at construction;
        only the eval fixture stages a case-specific space into an
        already-built runtime."""
        self._adopt_catalog(field_catalog)
        if attachments is not None:
            self._scoped = True
            self._adopt_attachments(attachments)
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
