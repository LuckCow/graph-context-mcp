"""Schema proposals: LLM-drafted, user-confirmed type changes (WP33, ADR 041).

The space-reflecting model (ADR 006) keeps the USER the owner of the
space's vocabulary -- the server reflects types, never invents them. This
service is the one sanctioned crack in that wall, and it keeps ownership
where it was: the LLM may *draft* a schema change (a new type, or new
scalar properties on an existing type) in conversation, but nothing
touches the space until the user has seen the exact change and confirmed
it, whereupon the model applies the stored proposal by id.

The confirmation is MECHANICAL and the LLM is not in the loop (ADR 041
v2): the model has no apply action at all. A propose_* call lands the
draft in this ledger AND in :attr:`SchemaProposals.drafted`, which the
pipeline drains after the turn into ``confirm`` reply events; the
Anytype transport posts each as its OWN message -- rendered from the
STORED proposal, never the model's paraphrase -- and remembers the
message id. When a human (any non-bot account identity, quirk C12)
reacts 👍 on that message, the harness calls :meth:`apply` directly --
no model turn; 👎 discards the draft the same way. Surfaces without a
reaction channel (the bare MCP server, CLI, Discord) can draft and
cancel but never apply; their confirm text says so.

Proposals are deliberately session-scoped and in-memory: they are DRAFTS
mid-conversation, not records. A restart clears them (a 👍 on a
pre-restart confirm message is silently inert; the model simply
re-proposes); an applied change needs no proposal record because the
minted type/properties ARE the durable outcome, attributed like any
other write.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count

from graph_context.domain import schema
from graph_context.domain.models import (
    PropertyDraft,
    validate_property_drafts,
)
from graph_context.errors import (
    GraphContextError,
    SchemaChangeConflict,
    UnknownNodeType,
)
from graph_context.ports.graph_repository import GraphRepository

# Pending drafts are working memory for ONE conversation thread; more
# than a handful means the model is hoarding instead of resolving.
MAX_PENDING_PROPOSALS = 5

NEW_TYPE = "new_type"
EXTEND_TYPE = "extend_type"


@dataclass(frozen=True, slots=True)
class SchemaProposal:
    """One drafted schema change, awaiting the user's confirmation."""

    id: str
    kind: str  # NEW_TYPE | EXTEND_TYPE
    type_name: str
    properties: tuple[PropertyDraft, ...]
    plural: str = ""
    reason: str = ""

    def summary(self) -> list[str]:
        """Render the exact change, one line per element -- what the model
        must show the user before asking for a yes."""
        if self.kind == NEW_TYPE:
            plural = f", plural {self.plural!r}" if self.plural else ""
            head = f"NEW TYPE {self.type_name!r}{plural}"
        else:
            head = f"NEW PROPERTIES on existing type {self.type_name!r}"
        count_note = (
            f" -- {len(self.properties)} propert"
            f"{'y' if len(self.properties) == 1 else 'ies'}:"
            if self.properties
            else " -- no properties (bare type)"
        )
        lines = [head + count_note]
        lines.extend(f"  - {draft.render_hint()}" for draft in self.properties)
        if self.reason:
            lines.append(f"  reason: {self.reason}")
        return lines

    def confirm_text(self) -> str:
        """The harness-authored confirmation body (ADR 041 v2) --
        rendered from the STORED draft so the human always confirms the
        exact change, never the model's paraphrase. Instruction-free:
        each transport appends its own (the Anytype chat offers the 👍
        reaction; surfaces without one say where confirmation lives)."""
        return "\n".join([f"Schema proposal {self.id}:", *self.summary()])


class SchemaProposals:
    """The per-session proposal ledger: draft, show, confirm, apply.

    Default-constructible (it holds no dependencies) so ``Services`` can
    carry one per session; the repository arrives per call, typed against
    the port.
    """

    def __init__(self) -> None:
        self._pending: dict[str, SchemaProposal] = {}
        self._seq = count(1)
        # Drafts minted THIS turn, awaiting their confirm messages.
        # Turn-scoped like Services.outbox: the pipeline clears it as a
        # turn starts and drains it into confirm reply events after the
        # last decision.
        self.drafted: list[SchemaProposal] = []

    def pending(self) -> tuple[SchemaProposal, ...]:
        return tuple(self._pending.values())

    def drain_drafted(self) -> tuple[SchemaProposal, ...]:
        """This turn's new drafts, cleared on read (the pipeline turns
        them into confirm events exactly once)."""
        drafted, self.drafted[:] = tuple(self.drafted), []
        return drafted

    def propose_type(
        self,
        repository: GraphRepository,
        name: str,
        *,
        plural: str = "",
        properties: tuple[PropertyDraft, ...] = (),
        reason: str = "",
    ) -> SchemaProposal:
        schema.validate_type_name(name)
        validate_property_drafts(properties)
        taken = {t.lower() for t in repository.known_node_types()}
        if name.strip().lower() in taken:
            raise SchemaChangeConflict(
                f"a type matching {name.strip()!r} already exists in this "
                "space; propose new properties on it instead "
                "(action='propose_fields')"
            )
        return self._stash(SchemaProposal(
            id=self._mint_id(), kind=NEW_TYPE, type_name=name.strip(),
            plural=plural.strip(), properties=tuple(properties),
            reason=reason.strip(),
        ))

    def propose_fields(
        self,
        repository: GraphRepository,
        type_identifier: str,
        properties: tuple[PropertyDraft, ...],
        *,
        reason: str = "",
    ) -> SchemaProposal:
        if not properties:
            raise GraphContextError(
                "propose_fields needs at least one property "
                "(properties=[{'name': ..., 'format': ...}, ...])"
            )
        validate_property_drafts(properties)
        # Best-effort existence check now (the repository re-checks
        # authoritatively at apply): a type the backend lists is certainly
        # fine; one it can also resolve by role passes at apply time.
        identifier = type_identifier.strip()
        known = repository.known_node_types()
        listed = identifier.lower() in {t.lower() for t in known}
        if not listed and repository.role_for(identifier) is None:
            raise UnknownNodeType(identifier, tuple(sorted(known)))
        return self._stash(SchemaProposal(
            id=self._mint_id(), kind=EXTEND_TYPE, type_name=identifier,
            properties=tuple(properties), reason=reason.strip(),
        ))

    def cancel(self, proposal_id: str) -> SchemaProposal:
        proposal = self._pending.pop(self._resolve(proposal_id))
        # A draft cancelled before its confirm message posted must not
        # still post one (same-turn propose-then-cancel).
        if proposal in self.drafted:
            self.drafted.remove(proposal)
        return proposal

    async def apply(
        self, repository: GraphRepository, proposal_id: str
    ) -> tuple[SchemaProposal, str]:
        """Execute a user-confirmed proposal; returns it with the type's
        display name as the backend reports it.

        HARNESS-ONLY (ADR 041 v2): the one caller is the Anytype
        transport's reaction handler -- the schema tool exposes no apply
        action, which is what makes the confirmation a guarantee rather
        than a norm."""
        proposal = self._pending[self._resolve(proposal_id)]
        if proposal.kind == NEW_TYPE:
            type_name = await repository.create_type(
                proposal.type_name,
                plural=proposal.plural,
                properties=proposal.properties,
            )
        else:
            type_name = await repository.add_type_properties(
                proposal.type_name, proposal.properties
            )
        del self._pending[proposal.id]
        return proposal, type_name

    def _stash(self, proposal: SchemaProposal) -> SchemaProposal:
        self._pending[proposal.id] = proposal
        self.drafted.append(proposal)
        return proposal

    def _mint_id(self) -> str:
        if len(self._pending) >= MAX_PENDING_PROPOSALS:
            held = ", ".join(self._pending)
            raise GraphContextError(
                f"already {MAX_PENDING_PROPOSALS} proposals pending "
                f"({held}); apply or cancel one first"
            )
        return f"p{next(self._seq)}"

    def _resolve(self, proposal_id: str) -> str:
        wanted = proposal_id.strip()
        if not wanted and len(self._pending) == 1:
            return next(iter(self._pending))
        if wanted in self._pending:
            return wanted
        held = ", ".join(self._pending) or "none"
        raise GraphContextError(
            f"no pending proposal {wanted!r} (pending: {held}); "
            "draft one with action='propose_type' or 'propose_fields'"
        )
