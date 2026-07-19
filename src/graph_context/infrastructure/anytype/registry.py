"""SpaceRegistry: the space's live types and relations.

The space-reflecting model needs to know what types and relation properties
already exist in the user's Anytype space so that:

* reads can render an object's native type *display name* and resolve its
  semantic :class:`Role` (``character`` -> Character / Role.CHARACTER);
* writes can resolve a requested type identifier or relation *label* to a
  concrete, existing key (reuse), and detect labels with no match (which the
  repository surfaces for approval rather than auto-creating).

Built once per hydrate from two cheap paged sweeps (``GET /types`` and
``GET /properties``) and refreshed on resync.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from graph_context.domain import schema
from graph_context.domain.schema import Role
from graph_context.infrastructure.anytype import mapping
from graph_context.infrastructure.anytype.client import AnytypeClient


@dataclass(frozen=True, slots=True)
class PropertyInfo:
    key: str
    name: str
    format: str
    # Property object id -- required by the tags routes (ADR 012), which
    # reject property keys. "" for entries built before ids mattered.
    id: str = ""


@dataclass(frozen=True, slots=True)
class TypeInfo:
    key: str
    name: str
    # Type object id -- required by the templates route
    # (GET /types/{typeId}/templates), which is keyed by id, not key. "" for
    # entries built before ids mattered.
    id: str = ""
    # The type's own properties as GET /types returns them (ADR 023) --
    # the per-type half of the catalog shown to the LLM. Space-level
    # properties never attached to a type are not in here; writes still
    # match space-wide via field_property.
    properties: tuple[PropertyInfo, ...] = ()


@dataclass
class SpaceRegistry:
    """A snapshot of the space's types and relation properties."""

    properties_by_key: dict[str, PropertyInfo] = field(default_factory=dict)
    types_by_key: dict[str, TypeInfo] = field(default_factory=dict)  # key -> TypeInfo
    role_overrides: dict[str, Role] = field(default_factory=dict)
    # Extra property keys hidden from field reflection (GC_FIELD_DENYLIST);
    # merged with mapping.SYSTEM_PROPERTY_DENYLIST in reflects_field().
    hidden_field_keys: frozenset[str] = frozenset()
    # The profile-declared timeline property (ADR 015): surfaced as
    # Node.story_time, so it must not ALSO reflect into fields.
    timeline_key: str = mapping.PROP_STORY_TIME

    # -- types ----------------------------------------------------------

    def type_name(self, key: str) -> str:
        """Display name for a type key (falls back to the key itself)."""
        info = self.types_by_key.get(key)
        return info.name if info else key

    def type_id_for(self, key: str) -> str | None:
        """Type object id for a key (needed by the templates route), or None."""
        info = self.types_by_key.get(key)
        return info.id if info and info.id else None

    def role_for(self, key: str) -> Role | None:
        return schema.resolve_role(key, self.role_overrides)

    def type_key_for(self, identifier: str) -> str | None:
        """Resolve a requested type identifier to an existing type key.

        Matches (in order) an exact type key, a type display name, or -- via
        role -- a type whose resolved role equals the named role/role-keyword.
        Returns ``None`` when nothing in the space matches.
        """
        target = identifier.strip().lower()
        for key in self.types_by_key:
            if key.lower() == target:
                return key
        for key, info in self.types_by_key.items():
            if info.name.strip().lower() == target:
                return key
        role = schema.resolve_role(identifier, self.role_overrides)
        if role is not None:
            for key in self.types_by_key:
                if self.role_for(key) is role:
                    return key
        return None

    def known_node_types(self) -> frozenset[str]:
        """Type display names available as create targets (non-infra)."""
        return frozenset(
            info.name
            for key, info in self.types_by_key.items()
            if self.role_for(key) not in schema.INFRA_ROLES
        )

    # -- relations ------------------------------------------------------

    def label_for(self, key: str) -> str:
        return mapping.clean_label(key)

    def key_for_label(self, label: str) -> str | None:
        """Resolve a relation *label* to an existing ``objects`` property key.

        Case-insensitive match on the cleaned label, the raw key, or the
        property's display name -- the display name is what the space's
        human (and therefore the LLM) sees, and ``field_property`` already
        matches it for scalars ("Linked Projects" must resolve as well as
        ``linked_projects``). Returns ``None`` when no existing relation
        matches (the writer then surfaces it for approval).
        """
        target = label.strip().lower()
        for key, info in self.properties_by_key.items():
            if info.format != "objects" or key in mapping.SYSTEM_RELATION_DENYLIST:
                continue
            if (
                mapping.clean_label(key).lower() == target
                or key.lower() == target
                or info.name.strip().lower() == target
            ):
                return key
        return None

    def known_edge_labels(self) -> frozenset[str]:
        """Relation labels available to reuse (for error suggestions)."""
        return frozenset(
            mapping.clean_label(key)
            for key, info in self.properties_by_key.items()
            if info.format == "objects" and key not in mapping.SYSTEM_RELATION_DENYLIST
        )

    def register_property(self, info: PropertyInfo) -> None:
        """Record a newly created relation so later writes can reuse it."""
        self.properties_by_key[info.key] = info

    def register_type(self, info: TypeInfo) -> None:
        """Record a created/re-fetched type so this session can use it
        without a resync (WP33) -- the type-level ``register_property``.
        The type's properties join the space-wide vocabulary too (a type
        create/update mints its inline properties as real space
        properties)."""
        self.types_by_key[info.key] = info
        for prop in info.properties:
            self.properties_by_key.setdefault(prop.key, prop)

    # -- scalar fields (ADR 012) -----------------------------------------

    def reflects_field(self, key: str, fmt: str) -> bool:
        """Should this property surface in ``Node.fields``?

        Scalar formats only; excludes ``gc_`` keys (first-class or
        server-managed) -- except the deliberately reflected surface
        (``GC_REFLECTED_FIELD_KEYS``: Scheduled Events per ADR 027,
        attribution stamps per ADR 028), which reads from and writes to
        real properties like any native field -- the built-in
        ``description`` (the summary channel, ADR 011), the census-based
        system-noise denylist, and any space-specific keys the user
        silenced via ``GC_FIELD_DENYLIST``.
        """
        return (
            fmt in mapping.REFLECTED_FIELD_FORMATS
            and (
                not key.startswith(mapping.GC_PREFIX)
                or key in mapping.GC_REFLECTED_FIELD_KEYS
            )
            and key != mapping.PROP_SUMMARY
            and key != self.timeline_key  # surfaced as story_time (ADR 015)
            and key not in mapping.SYSTEM_PROPERTY_DENYLIST
            and key not in self.hidden_field_keys
        )

    def reflectable_type_properties(self, type_key: str) -> tuple[PropertyInfo, ...]:
        """The type's own properties that are usable as ``fields`` keys."""
        info = self.types_by_key.get(type_key)
        if info is None:
            return ()
        return tuple(
            prop for prop in info.properties
            if self.reflects_field(prop.key, prop.format)
        )

    def reflectable_properties(self) -> tuple[PropertyInfo, ...]:
        """Every space property usable as a ``fields`` key (the write-match
        universe of :meth:`field_property`)."""
        return tuple(
            info for key, info in sorted(self.properties_by_key.items())
            if self.reflects_field(key, info.format)
        )

    def field_property(self, identifier: str) -> PropertyInfo | None:
        """Resolve a ``fields`` key to a reflectable scalar property.

        Case-insensitive match on key or display name -- the write-side
        mirror of :meth:`reflects_field` (an unmatched key is surfaced for
        approval by the repository).
        """
        target = identifier.strip().lower()
        for key, info in self.properties_by_key.items():
            if not self.reflects_field(key, info.format):
                continue
            if key.lower() == target or info.name.strip().lower() == target:
                return info
        return None


async def load_registry(
    client: AnytypeClient,
    extra_role_overrides: Mapping[str, Role] | None = None,
    hidden_field_keys: frozenset[str] = frozenset(),
    timeline_key: str = mapping.PROP_STORY_TIME,
) -> SpaceRegistry:
    """Build a registry from the space's live types and properties.

    ``extra_role_overrides`` carries the active DomainProfile's type-key ->
    Role additions (WP5). ``hidden_field_keys`` carries GC_FIELD_DENYLIST
    (ADR 012).
    """
    types_by_key: dict[str, TypeInfo] = {}
    async for type_obj in client.list_types():
        key = type_obj.get("key")
        if key:
            type_properties = tuple(
                PropertyInfo(
                    key=prop["key"], name=prop.get("name", prop["key"]),
                    format=prop.get("format", ""), id=prop.get("id", ""),
                )
                for prop in type_obj.get("properties", [])
                if prop.get("key")
            )
            types_by_key[key] = TypeInfo(
                key=key, name=type_obj.get("name", key), id=type_obj.get("id", ""),
                properties=type_properties,
            )
    properties_by_key: dict[str, PropertyInfo] = {}
    async for prop in client.list_properties():
        key = prop.get("key")
        if key:
            properties_by_key[key] = PropertyInfo(
                key=key, name=prop.get("name", key),
                format=prop.get("format", ""), id=prop.get("id", ""),
            )
    return SpaceRegistry(
        properties_by_key=properties_by_key,
        types_by_key=types_by_key,
        role_overrides=dict(extra_role_overrides or {}),
        hidden_field_keys=hidden_field_keys,
        timeline_key=timeline_key,
    )
