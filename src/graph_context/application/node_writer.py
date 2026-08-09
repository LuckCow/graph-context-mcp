"""Use-case: writing nodes (the ``create_node`` / ``update_node`` tools).

This is where the proposal's write-side business rules live, and *only*
here -- the repository persists what it is told, the domain validates
structure, and this service decides policy:

    * creation invariants (summary required, Events need a timeline
      position) via :func:`schema.validate_new_node`;
    * the summary-staleness rule: any update without a fresh summary
      flags ``summary_stale = True`` (relationship-only changes count --
      the one-liner may no longer reflect who the node is connected to);
    * every touched node is recorded into the session's recent history,
      which is what makes session-default starts work downstream (the
      curated working set is only ever changed by explicit ``hold`` calls);
    * the ADR 042 scope rule: a ``create_missing_properties`` declaration
      with ``scope="type"`` drafts an EXTEND_TYPE schema proposal (the
      ADR 041 user-confirmed flow) AFTER the write lands -- the value is
      durable immediately either way, and only the human's 👍 attaches
      the property to the type. A drafting failure (ledger cap, conflict)
      degrades to a returned warning; it never unwinds the write.

Services receive their dependencies through the constructor (constructor
injection); nothing here knows whether the repository is the in-memory
fake or the Anytype adapter.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from graph_context.application.mutation_journal import MutationJournal, NullJournal
from graph_context.application.schema_proposals import SchemaProposal, SchemaProposals
from graph_context.domain import schema
from graph_context.domain.models import (
    Edge,
    LinkSpec,
    Node,
    NodeDraft,
    NodeId,
    PropertyDeclaration,
    PropertyDraft,
    TimelineValue,
    validate_property_declarations,
)
from graph_context.domain.session import SessionState
from graph_context.errors import GraphContextError
from graph_context.ports.graph_repository import GraphRepository


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """A write's result plus its schema side-channel (ADR 042).

    ``drafted`` carries the EXTEND_TYPE proposal minted for
    ``scope="type"`` declarations (at most one per write; empty for
    instance-only writes); ``warnings`` carries anything that degraded
    (the write itself is always durable when this returns).
    """

    node: Node
    drafted: tuple[SchemaProposal, ...] = ()
    warnings: tuple[str, ...] = ()


class SectionGuard(Protocol):
    """Locked-section enforcement seam (WP42, ADR 049): consulted on
    tracked-type body updates. Structural on purpose -- the writer never
    imports the historian; the composition root injects it."""

    def check_body_update(self, node_id: NodeId, new_body: str) -> None:
        """Raise :class:`~graph_context.errors.LockedSectionsChanged`
        if ``new_body`` drops a locked section; return otherwise."""
        ...


class NodeWriter:
    """Composite, rule-enforcing writes against the story-world graph."""

    def __init__(
        self,
        repository: GraphRepository,
        session: SessionState,
        journal: MutationJournal | None = None,
        proposals: SchemaProposals | None = None,
        section_guard: SectionGuard | None = None,
    ) -> None:
        self._repository = repository
        self._session = session
        # WP7: writers report touched nodes at the source; the default
        # NullJournal keeps the MCP server's behavior unchanged.
        self._journal = journal or NullJournal()
        # ADR 042: scope="type" declarations draft into the session's
        # proposal ledger. None (bare construction, recorders' internal
        # writes) turns type-scope drafting into a warning.
        self._proposals = proposals
        # WP42: locked-section enforcement, one rule in one place. None
        # (bare MCP server -- no historian) means no enforcement, per
        # ADR 049's v1 scope.
        self._section_guard = section_guard

    async def create_node(
        self,
        draft: NodeDraft,
        links: Sequence[LinkSpec] = (),
        *,
        declarations: Mapping[str, PropertyDeclaration] | None = None,
        admitted_infra_roles: frozenset[schema.Role] = frozenset(),
    ) -> WriteOutcome:
        """Create a node and its initial links as one logical operation.

        ``admitted_infra_roles`` (ADR 045) carries the caller's meta
        privilege: infra-role targets are denied unless admitted -- the
        infra surfaces have dedicated owners (scheduler, rule engine,
        recorders) that write through the repository, never through here.
        """
        role = self._repository.role_for(draft.type)
        schema.validate_infra_write(
            role, draft.type, admitted_infra_roles,
            tuple(self._repository.known_node_types()),
        )
        schema.validate_new_node(
            role, draft.name, draft.summary, draft.story_time
        )
        declared = dict(declarations or {})
        written = set(draft.fields) | {link.edge_type for link in links}
        validate_property_declarations(written, declared)
        node = await self._repository.create_node(
            draft, links, create_missing=declared,
        )
        self._journal.created(node.id)
        for link in links:
            self._session.recent.record(link.other)
        self._session.touch(node.id)  # last, so the new node is most recent
        drafted, warnings = self._draft_type_scoped(node.type, declared)
        return WriteOutcome(node=node, drafted=drafted, warnings=warnings)

    async def update_node(
        self,
        node_id: NodeId,
        *,
        name: str | None = None,
        summary: str | None = None,
        description: str | None = None,
        story_time: TimelineValue | None = None,
        fields: Mapping[str, str] | None = None,
        add_links: Sequence[LinkSpec] = (),
        remove_links: Sequence[Edge] = (),
        declarations: Mapping[str, PropertyDeclaration] | None = None,
        admitted_infra_roles: frozenset[schema.Role] = frozenset(),
    ) -> WriteOutcome:
        """Apply field and link changes; flag staleness unless summary is fresh."""
        target = self._repository.graph.node(node_id)  # fail fast on bad id
        schema.validate_infra_write(
            target.role, target.type, admitted_infra_roles,
            tuple(self._repository.known_node_types()),
        )
        declared = dict(declarations or {})
        written = set(fields or {}) | {link.edge_type for link in add_links}
        validate_property_declarations(written, declared)
        if description is not None and self._section_guard is not None:
            self._section_guard.check_body_update(node_id, description)

        await self._repository.update_node(
            node_id,
            name=name,
            summary=summary,
            summary_stale=self._staleness_after_update(summary),
            # The tool surface says "description"; storage says "body"
            # (ADR 010) -- this is the one place the words meet.
            body=description,
            story_time=story_time,
            fields=fields,
            create_missing=declared,
        )
        for link in add_links:
            await self._repository.add_link(
                node_id, link,
                create_missing=_link_declaration(link.edge_type, declared),
            )
        for edge in remove_links:
            await self._repository.remove_link(edge)
            if edge.source != node_id:
                self._journal.modified(edge.source)

        self._journal.modified(node_id)
        self._session.touch(node_id)
        node = self._repository.graph.node(node_id)
        drafted, warnings = self._draft_type_scoped(node.type, declared)
        return WriteOutcome(node=node, drafted=drafted, warnings=warnings)

    def _draft_type_scoped(
        self, type_name: str, declared: Mapping[str, PropertyDeclaration]
    ) -> tuple[tuple[SchemaProposal, ...], tuple[str, ...]]:
        """One EXTEND_TYPE proposal covering every ``scope="type"``
        declaration of this write (ADR 042). Runs AFTER the repository
        write: the value is durable, so any drafting failure -- ledger
        cap, format conflict, no ledger wired -- degrades to a warning
        rather than unwinding a landed write. Drafting even when the
        property already existed space-wide is deliberate: attaching an
        existing unattached property IS the useful case, and an
        already-attached one applies as a retry-safe no-op."""
        type_scoped = [d for d in declared.values() if d.scope == "type"]
        if not type_scoped:
            return (), ()
        drafts = tuple(
            # PropertyDraft carries the DISPLAY name: the mint used it, so
            # the apply-side reuse lookup finds the same property.
            _as_draft(declaration) for declaration in type_scoped
        )
        if self._proposals is None:
            return (), (
                "scope='type' was requested but this surface has no "
                "proposal ledger; the property was created space-level "
                "only -- attach it to the type via the schema tool",
            )
        try:
            proposal = self._proposals.propose_fields(
                self._repository, type_name, drafts,
                reason="auto-drafted: a write declared scope='type'",
            )
        except GraphContextError as err:
            return (), (
                f"the value is saved, but the type-attach proposal could "
                f"not be drafted: {err}",
            )
        return (proposal,), ()

    @staticmethod
    def _staleness_after_update(summary: str | None) -> bool:
        """Proposal rule: updates without a new summary mark the node stale."""
        return summary is None


def _as_draft(declaration: PropertyDeclaration) -> PropertyDraft:
    return PropertyDraft(
        name=declaration.display_name, format=declaration.format
    )


def _link_declaration(
    label: str, declared: Mapping[str, PropertyDeclaration]
) -> PropertyDeclaration | None:
    """The declaration licensing this link label, if any (``objects``
    format only -- a scalar declaration never licenses a relation)."""
    target = label.strip().lower()
    for key, declaration in declared.items():
        if key.strip().lower() == target and declaration.format == "objects":
            return declaration
    return None
