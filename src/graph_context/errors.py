"""Exception hierarchy for graph-context-mcp.

Every error raised by this package derives from :class:`GraphContextError`,
so callers (ultimately the MCP tool layer) can catch one base class and
translate it into a structured tool-error response.

Conventions:
    * Domain rules raise :class:`SchemaViolation`.
    * Lookups raise :class:`NodeNotFound` rather than returning ``None`` --
      "node is missing" is exceptional inside a session that just referenced it.
    * Infrastructure adapters wrap transport failures in their own subclass
      (e.g. a future ``AnytypeApiError``) defined next to the adapter.
"""

from __future__ import annotations


class GraphContextError(Exception):
    """Base class for all errors raised by this package."""


class SchemaViolation(GraphContextError):
    """A write violated a node-creation invariant or a structural rule."""


class InfraWriteDenied(GraphContextError):
    """A write targeted a system-configuration (infra-role) type (ADR 045).

    Raised by :func:`graph_context.domain.schema.validate_infra_write`
    when a ``create_node``/``update_node`` targets an infra-role object
    the active mode's privilege does not admit. The message names the
    legitimate surfaces so the model self-corrects instead of retrying.
    """

    def __init__(
        self, type_name: str, role: str, known: tuple[str, ...] = ()
    ) -> None:
        self.type_name = type_name
        self.role = role
        hint = (
            f" Types you can write: {', '.join(sorted(known))}."
            if known else ""
        )
        super().__init__(
            f"{type_name!r} is system configuration ({role}), not "
            "story/world data; this mode cannot create or modify it. "
            "Activity Mode objects are editable only by a mode with "
            "meta-inspection (the Space Setup mode) or directly in "
            "Anytype; Scheduled Events and Automation Rules have their "
            f"own tools (schedule, automation).{hint}"
        )


class ApprovalRequired(GraphContextError):
    """A write needs a new space-level type or relation the user must approve.

    The space-reflecting model reuses existing types/relations; when a write
    asks for one that does not exist yet, the writer raises this instead of
    silently creating it, so new vocabulary is an explicit, user-approved act.
    """


class UnknownNodeType(ApprovalRequired):
    """``create_node`` requested a type with no match in the space."""

    def __init__(self, requested: str, known: tuple[str, ...] = ()) -> None:
        self.requested = requested
        self.known = tuple(known)
        hint = f" Known types: {', '.join(sorted(self.known))}." if self.known else ""
        super().__init__(f"no type in this space matches {requested!r}.{hint}")


class UnknownRelationLabel(ApprovalRequired):
    """A link used a relation label the write's type scope does not admit.

    With a ``type_name`` the message renders the ADR 047 sections: the
    type's own relations (bare-usable), then space relations not attached
    to the type -- reusable through the same ``create_missing_properties``
    declaration that would mint a new one (a declared label matching an
    existing relation attaches it instead of minting). The bare form
    (open-mode fake, legacy raisers) keeps the space-wide wording.
    """

    def __init__(
        self,
        label: str,
        known: tuple[str, ...] = (),
        *,
        type_name: str = "",
        attached: tuple[str, ...] = (),
        unattached: tuple[str, ...] = (),
    ) -> None:
        self.label = label
        self.known = tuple(known)
        self.type_name = type_name
        self.attached = tuple(attached)
        self.unattached = tuple(unattached)
        declaration = (
            f"create_missing_properties={{{label!r}: {{'format': 'objects', "
            "'scope': 'instance'|'type'}}}"
        )
        if not type_name:
            hint = (
                f" Existing relations: {', '.join(sorted(self.known))}."
                if self.known else ""
            )
            super().__init__(
                f"no existing relation matches label {label!r}; declare it in "
                f"{declaration} to create it.{hint}"
            )
            return
        parts = [f"no relation on {type_name} matches label {label!r}."]
        if self.attached:
            parts.append(
                f"Relations on {type_name}: {', '.join(sorted(self.attached))}."
            )
        if self.unattached:
            parts.append(
                f"Space relations NOT attached to {type_name} (a declaration "
                "naming one attaches it instead of minting): "
                f"{', '.join(sorted(self.unattached))}."
            )
        parts.append(
            f"Declare it in {declaration} to attach an existing relation or "
            "create a new one ('type' scope also drafts the attach-to-type "
            "proposal for the user's 👍)."
        )
        super().__init__(" ".join(parts))


class UnknownFieldKey(ApprovalRequired):
    """A ``properties`` key the write's type scope does not admit (ADR 047).

    Values must land in real store properties -- never a hidden extras
    blob (ADR 028) -- and bare keys resolve only against the target
    type's attached properties (plus, on update, the object's own), so an
    unadmitted key stops the write. The message lists the type's own
    vocabulary first (bare-usable), then space vocabulary NOT attached to
    the type -- reusable through the same explicit
    ``create_missing_properties`` declaration that would mint a genuinely
    new property (a declared key matching an existing same-format space
    property attaches it instead of minting).

    ``relation_label``: the key DID match a relation the scope admits --
    in this system an edge (ADR 006), whose value is a node reference. The
    message then teaches the relation value shape instead of listing scalar
    properties or offering a declaration (which would try to mint a scalar
    shadowing the relation). Live-caught: a space's "Assignee" relation was
    invisible to a model that only knew scalar keys, and the old message
    sent it further astray.

    ``space_match_name``/``space_match_format``: the key exactly matches
    space vocabulary that is NOT attached to the type -- the incident
    shape (ADR 047: an unattached near-duplicate relation kept resolving
    bare). The message states that fact and the attach gesture first.
    """

    def __init__(
        self,
        key: str,
        type_name: str,
        type_properties: tuple[str, ...] = (),
        type_relations: tuple[str, ...] = (),
        unattached_properties: tuple[str, ...] = (),
        unattached_relations: tuple[str, ...] = (),
        formats: tuple[str, ...] = (),
        relation_label: str = "",
        space_match_name: str = "",
        space_match_format: str = "",
    ) -> None:
        self.key = key
        self.type_name = type_name
        self.type_properties = tuple(type_properties)
        self.type_relations = tuple(type_relations)
        self.unattached_properties = tuple(unattached_properties)
        self.unattached_relations = tuple(unattached_relations)
        self.relation_label = relation_label
        self.space_match_name = space_match_name
        self.space_match_format = space_match_format
        if relation_label:
            super().__init__(
                f"{key!r} is an objects-format RELATION here -- an "
                "edge, not a scalar. Its value must be an existing node's id "
                f"or name (or a list of them): properties="
                f"{{{relation_label!r}: '<target node id or name>'}}."
            )
            return
        parts = [f"no property on {type_name} matches key {key!r}."]
        if space_match_name:
            parts.append(
                f"{key!r} matches the space property {space_match_name!r} "
                f"(format {space_match_format!r}) which is NOT attached to "
                f"{type_name}; to use it here, resend with "
                f"create_missing_properties={{{key!r}: {{'format': "
                f"{space_match_format!r}, 'scope': 'instance'|'type'}}}} -- "
                "it will be reused, not duplicated ('type' scope also "
                "drafts the attach-to-type proposal for the user's 👍)."
            )
        if self.type_properties:
            parts.append(
                f"Properties on {type_name}: {', '.join(self.type_properties)}."
            )
        if self.type_relations:
            parts.append(
                f"Relations on {type_name} (each value is a node id/name, "
                f"or a list): {', '.join(sorted(self.type_relations))}."
            )
        if self.unattached_properties:
            parts.append(
                f"Space properties NOT attached to {type_name} (declare in "
                "create_missing_properties to reuse): "
                f"{', '.join(self.unattached_properties)}."
            )
        if self.unattached_relations:
            parts.append(
                f"Space relations NOT attached to {type_name} (declare with "
                "format 'objects' to reuse): "
                f"{', '.join(sorted(self.unattached_relations))}."
            )
        parts.append(
            "To use the type's own vocabulary, use its name as the "
            "properties key; to attach existing space vocabulary or create "
            "a NEW property, resend with create_missing_properties="
            f"{{{key!r}: '<format>'}} (or {{'format': ..., 'scope': "
            "'instance'|'type'}} -- 'type' when every object of the type "
            "should carry it)"
        )
        if formats:
            parts.append(f"(formats: {', '.join(sorted(formats))}).")
        super().__init__(" ".join(parts))


class SchemaChangeConflict(GraphContextError):
    """A schema change collides with what the space already has (WP33).

    Raised by ``GraphRepository.create_type`` when the proposed type name
    already resolves to an existing type, and by both schema-change
    methods when a proposed property name matches an existing property
    under a DIFFERENT format (formats are immutable, quirk A12) or an
    existing relation (an edge must never gain a scalar shadow, ADR 006).
    The message is a prompt: it names the collision and the way out.
    """


class NodeNotFound(GraphContextError):
    """A referenced node id (or name) does not exist in the graph.

    The message doubles as a prompt: an identifier may be an Anytype id *or*
    a node name (the tool layer resolves names transparently), so a miss
    points the LLM at the two ways to discover a real id.
    """

    def __init__(self, node_id: str, suggestions: str = "") -> None:
        message = (
            f"no node matches {node_id!r} by id or name; call find_node to "
            "search by name, or context action='overview' for entry-point ids."
        )
        if suggestions:
            # ADR 016: the resolver's miss becomes a search surface. The
            # candidates SUGGEST -- the model must pick an id; a fuzzy match
            # never resolves silently (ADR 014 non-feature).
            message += "\nClosest by meaning:\n" + suggestions
        super().__init__(message)
        self.node_id = node_id


class AmbiguousNodeName(GraphContextError):
    """A name resolved to more than one node; the caller must disambiguate.

    Raised by name resolution (never by an exact id, which is unique). The
    message lists every candidate with its id so the LLM can retry with an
    exact id -- same "errors are prompts" convention as the schema errors.
    """

    def __init__(
        self, query: str, candidates: tuple[tuple[str, str, str], ...]
    ) -> None:
        self.query = query
        self.candidates = candidates
        listing = "; ".join(
            f"{name} ({type_}, id={node_id})"
            for name, type_, node_id in candidates
        )
        super().__init__(
            f"{query!r} matches {len(candidates)} nodes: {listing}. "
            "Retry with an exact id, or a more specific name."
        )


class NoDefaultStart(GraphContextError):
    """A query relied on the session default, but nothing is held or recent."""

    def __init__(self) -> None:
        super().__init__(
            "no start node given, nothing held in the working set, and "
            "nothing recently touched; pass an explicit start, hold a node "
            "first (context action='hold'), or call context "
            "action='overview' to list entry-point node ids."
        )
