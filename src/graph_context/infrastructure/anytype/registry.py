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


@dataclass
class SpaceRegistry:
    """A snapshot of the space's types and relation properties."""

    properties_by_key: dict[str, PropertyInfo] = field(default_factory=dict)
    types_by_key: dict[str, str] = field(default_factory=dict)  # key -> display name
    role_overrides: dict[str, Role] = field(default_factory=dict)

    # -- types ----------------------------------------------------------

    def type_name(self, key: str) -> str:
        """Display name for a type key (falls back to the key itself)."""
        return self.types_by_key.get(key, key)

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
        for key, name in self.types_by_key.items():
            if name.strip().lower() == target:
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
            name
            for key, name in self.types_by_key.items()
            if self.role_for(key) not in schema.INFRA_ROLES
        )

    # -- relations ------------------------------------------------------

    def label_for(self, key: str) -> str:
        return mapping.clean_label(key)

    def key_for_label(self, label: str) -> str | None:
        """Resolve a relation *label* to an existing ``objects`` property key.

        Case-insensitive match on the cleaned label or the raw key. Returns
        ``None`` when no existing relation matches (the writer then surfaces
        it for approval).
        """
        target = label.strip().lower()
        for key, info in self.properties_by_key.items():
            if info.format != "objects" or key in mapping.SYSTEM_RELATION_DENYLIST:
                continue
            if mapping.clean_label(key).lower() == target or key.lower() == target:
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


async def load_registry(client: AnytypeClient) -> SpaceRegistry:
    """Build a registry from the space's live types and properties."""
    types_by_key: dict[str, str] = {}
    async for type_obj in client.list_types():
        key = type_obj.get("key")
        if key:
            types_by_key[key] = type_obj.get("name", key)
    properties_by_key: dict[str, PropertyInfo] = {}
    async for prop in client.list_properties():
        key = prop.get("key")
        if key:
            properties_by_key[key] = PropertyInfo(
                key=key, name=prop.get("name", key), format=prop.get("format", "")
            )
    return SpaceRegistry(
        properties_by_key=properties_by_key, types_by_key=types_by_key
    )
