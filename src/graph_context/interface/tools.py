"""Tool implementations: the v1 MCP surface, SDK-free.

``server.py`` registers thin FastMCP wrappers around these functions;
keeping the implementations here (plain async functions over a
:class:`Services` bundle) means they are testable in-process without an
MCP client, and the SDK never leaks below the composition root.

Two invariants every tool maintains -- enforced by ``guarded``, the one
wrapper everything goes through:

1. **Errors are prompts.** Any :class:`GraphContextError` is returned as
   ``ERROR: <message>`` -- its message is written for an LLM trying to
   self-correct, so parse failures must list the allowed values (see the
   ``_parse_*`` helpers). Unexpected exceptions are logged server-side and
   returned as a generic message: never leak stack traces into a story.
2. **Policy stays here.** e.g. `explore` excludes Prose/SessionContext by
   default (WP2 decision) -- the domain traversal remains policy-free.

(The per-response ``[project | focus | recent]`` context header was
removed 2026-07-06 as token waste. WP15 replaced the focus stack with
the LLM-curated working set + scratchpad, echoed once per orchestrator
turn instead of on every response; recent history still feeds traversal
defaults.)

Notes:
* `context` actions `set_project` / `resync`: resync is wired; project
  switching is a stub by design -- one server process = one space in v1
  (the repository is bound to a space id at construction). The stub's
  message explains that to the LLM. Revisit only with multi-space config.
* Writes call `_note_mutation(services)`, which drives the debounced
  SessionPersister wired in server.py's lifespan (a no-op when absent, as
  in the memory backend and most tests).
* Capture is the ORCHESTRATOR's job (WP7 auto-capture); the record_prose
  tool was removed 2026-07-04 -- the project is pre-deployment, so no
  vestigial surface is kept. CaptureRecorder is the service the harness
  calls, with the artifact type set by the active mode's CapturePolicy
  (ADR 015).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import Any

from graph_context.application.document_editor import DocumentEditor
from graph_context.application.node_writer import WriteOutcome
from graph_context.application.scheduler import Scheduler
from graph_context.application.schema_proposals import SchemaProposal
from graph_context.domain import revisions, rules, schema
from graph_context.domain.models import (
    Edge,
    Node,
    NodeDraft,
    NodeId,
    PropertyDraft,
)
from graph_context.domain.overview import build_overview
from graph_context.domain.query import (
    NodeQuery,
)
from graph_context.domain.schema import Role
from graph_context.domain.session import SCRATCHPAD_MAX_CHARS
from graph_context.domain.traversal import ExploreQuery
from graph_context.errors import GraphContextError, NodeNotFound
from graph_context.interface import presenters
from graph_context.interface.mode_config import slugify
from graph_context.interface.presenters import Detail
from graph_context.interface.services import OutboundFile, Services
from graph_context.interface.tool_args import (
    _edge_type_set,
    _node_type_set,
    _parse_detail,
    _parse_edge_type,
    _parse_hold_detail,
    _parse_node_type,
    _parse_order_by,
    _parse_predicates,
    _parse_properties,
    _resolve,
    _validate_query_type,
)

logger = logging.getLogger(__name__)

# WP2 decision: bookkeeping node *roles* never surface in traversal unless
# explicitly included. Tool-layer policy, not domain. (Intent joins via
# INFRA_ROLES-driven reader suppression; here the explore default.)
DEFAULT_EXPLORE_EXCLUDE_ROLES = frozenset(
    {Role.CAPTURE, Role.SESSION_CONTEXT, Role.INTENT}
)


# -- the one wrapper ------------------------------------------------------


def guarded(
    fn: Callable[..., Awaitable[str]],
) -> Callable[..., Awaitable[str]]:
    """GraphContextError -> actionable ERROR line; nothing else escapes.

    Also the single seam for structured per-call logging (WP2 deliverable):
    one INFO line per tool with name, ok/error outcome, and duration.
    Deliberately logs *no* payload -- prose text and summaries are a user's
    creative work and must never appear above DEBUG.
    """

    @wraps(fn)
    async def wrapper(services: Services, *args: Any, **kwargs: Any) -> str:
        start = time.perf_counter()
        outcome = "ok"
        try:
            body = await fn(services, *args, **kwargs)
        except GraphContextError as known:
            outcome = "error"
            body = f"ERROR: {known}"
        except Exception:  # never leak a traceback into a story
            outcome = "error"
            logger.exception("unexpected error in tool %s", fn.__name__)
            body = "ERROR: internal error; details were logged server-side."
        finally:
            logger.info(
                "tool=%s outcome=%s duration_ms=%.1f",
                fn.__name__, outcome, (time.perf_counter() - start) * 1000,
            )
        return body

    return wrapper


def is_error_result(result: str) -> bool:
    """Whether a rendered tool result is :func:`guarded`'s error form.

    The ``ERROR: `` prefix rule lives in this file; consumers (the
    pipeline's activity observer, WP19) ask instead of re-spelling it.
    """
    return result.startswith("ERROR: ")


async def _note_mutation(services: Services) -> None:
    if services.persister is not None:
        await services.persister.note_mutation()


# -- tools ------------------------------------------------------------------


def _render_session_echo(services: Services) -> list[str]:
    """The `get` action's session section (WP15): scratchpad + working set
    + recent trail, names resolved against the live graph (vanished nodes
    are skipped, never crash a response)."""
    session = services.session
    graph = services.repository.graph
    working_set = session.working_set

    def name_of(node_id: NodeId) -> str | None:
        if not graph.has_node(node_id):
            return None
        node = graph.node(node_id)
        return f"{node.name} ({node.type}, id={node.id})"

    lines = [f"scratchpad: {session.scratchpad or '(empty)'}"]
    held = [
        f"- {label} [{entry.detail.value}]"
        for entry in working_set.entries
        if (label := name_of(entry.node_id))
    ]
    if held:
        full_used = sum(
            1 for e in working_set.entries if e.detail is Detail.FULL
        )
        lines.append(
            f"working set ({full_used}/{working_set.full_slots} full, "
            f"{len(working_set) - full_used}/{working_set.summary_slots} "
            "summary slots):"
        )
        lines.extend(held)
    else:
        lines.append(
            "working set: empty -- keep a node in every turn's context "
            "with context action='hold'."
        )
    recent = [n for i in session.recent.items if (n := name_of(i))]
    if recent:
        lines.append(f"recent: {', '.join(r.split(' (')[0] for r in recent)}")
    return lines


async def resync_out_of_band(services: Services) -> frozenset[NodeId]:
    """Pull edits made directly in Anytype into the index; return changed ids.

    The single resync path -- the context tool's action, find_node's
    miss retry, and the transports' periodic refresh all come through
    here, so the embedding cache never falls out of step with the graph.
    """
    changed = await services.repository.resync()
    if services.projector is not None and changed:
        await services.projector.refresh(changed)
    return changed


@guarded
async def context_tool(
    services: Services,
    action: str = "get",
    node_id: str = "",
    project: str = "",
    text: str = "",
    detail: str = "",
) -> str:
    graph = services.repository.graph
    session = services.session
    if action == "get":
        # Count only story nodes -- the managed SessionContext node and Prose
        # passages are bookkeeping and would otherwise inflate an empty world.
        story = [n for n in graph.nodes() if n.role not in schema.INFRA_ROLES]
        stale = sum(1 for n in story if n.summary_stale)
        lines = [
            f"graph: {len(story)} nodes, {graph.edge_count()} edges, "
            f"{stale} stale summaries. "
            "Call context action='overview' for entry-point node ids.",
            *_render_session_echo(services),
        ]
        return "\n".join(lines)
    if action in {"overview", "map"}:
        # Derived cold-start map: per-type counts + highest-degree hub nodes,
        # each with an id to start exploring from, plus the space's property
        # catalog (ADR 023) so writes reuse existing properties as fields
        # keys. Empty graph -> guidance, not an error (a fresh session
        # should get something actionable).
        return presenters.render_overview(
            build_overview(graph),
            services.repository.field_catalog(
                include_roles=services.visible_infra_roles
            ),
        )
    if action == "resync":
        changed = await resync_out_of_band(services)
        if not changed:
            return "resync: no out-of-band changes."
        names = sorted(
            graph.node(i).name for i in changed if graph.has_node(i)
        )
        removed = len(changed) - len(names)
        suffix = f" ({removed} removed)" if removed else ""
        return (
            f"resync: {len(changed)} node(s) changed outside this "
            f"session{suffix}: {', '.join(names)}"
        )
    if action == "set_project":
        # v1: one server process = one space; the label is cosmetic.
        services.session.project = project or services.session.project
        return (
            "project label updated. Note: this server is bound to one "
            "Anytype space; switching spaces means restarting the server "
            "with a different ANYTYPE_SPACE_ID."
        )
    if action == "note":
        if len(text) > SCRATCHPAD_MAX_CHARS:
            raise GraphContextError(
                f"scratchpad is limited to {SCRATCHPAD_MAX_CHARS} characters "
                f"(got {len(text)}); condense it -- durable facts belong in "
                "the graph, not the scratchpad"
            )
        session.scratchpad = text.strip()
        # Flush immediately: the scratchpad is the model's cross-turn
        # memory; losing it to the mutation debounce defeats the feature.
        if services.persister is not None:
            await services.persister.flush()
        if not session.scratchpad:
            return "scratchpad cleared."
        return (
            f"scratchpad replaced ({len(session.scratchpad)} chars); it is "
            "echoed at the start of your next turn."
        )
    if action == "hold":
        if not node_id:
            raise GraphContextError(
                "action 'hold' requires node_id (a node id or name)"
            )
        level = _parse_hold_detail(detail)
        node_id = await _resolve(services, node_id)  # accept a name first
        outcome = session.working_set.hold(node_id, level)
        parts = [f"holding {graph.node(node_id).name} [{level.value}]"]
        parts.extend(
            f"demoted to summaries ({session.working_set.full_slots} full "
            f"slots): {graph.node(i).name}"
            for i in outcome.demoted if graph.has_node(i)
        )
        parts.extend(
            f"released ({session.working_set.summary_slots} summary slots): "
            f"{graph.node(i).name}"
            for i in outcome.evicted if graph.has_node(i)
        )
        return "; ".join(parts) + "."
    if action == "release":
        if not node_id:
            raise GraphContextError(
                "action 'release' requires node_id (a node id or name)"
            )
        try:
            resolved = await _resolve(services, node_id)
        except NodeNotFound:
            # The node may have been deleted out from under the hold; a
            # raw-id release must still work so the set can be tidied.
            if session.working_set.release(node_id):
                return f"released {node_id} (node no longer exists)."
            raise
        if session.working_set.release(resolved):
            return f"released {graph.node(resolved).name}."
        return f"{graph.node(resolved).name} was not held."
    if action == "clear":
        session.working_set.clear()
        return (
            "working set cleared. The scratchpad is kept; clear it with "
            "action='note', text=''."
        )
    raise GraphContextError(
        f"unknown action {action!r}; allowed: get, overview, resync, "
        "set_project, note, hold, release, clear"
    )


_PROMPT_EXCERPT_CHARS = 160  # list action: enough to recognize, not a wall


def _clock_line(scheduler: Scheduler) -> str:
    return (
        "server local time: "
        f"{scheduler.now().isoformat(sep=' ', timespec='minutes')}"
    )


def _schedule_excerpt(text: str) -> str:
    excerpt = text.strip().replace("\n", " ")
    if len(excerpt) > _PROMPT_EXCERPT_CHARS:
        excerpt = excerpt[:_PROMPT_EXCERPT_CHARS] + "…"
    return excerpt


@guarded
async def schedule_tool(
    services: Services,
    action: str = "list",
    name: str = "",
    schedule: str = "",
    prompt: str = "",
    message: str = "",
    mode: str = "",
    document_type: str = "",
    node_id: str = "",
) -> str:
    scheduler = services.scheduler
    if action == "set":
        # Modes are addressed by slug everywhere (registry keys, /mode);
        # slugify here so "Space Setup" and "space_setup" both land.
        # document_type is NOT slugified -- it's a node type name like
        # "Report", matched against the space's types at fire time.
        mode_slug = slugify(mode) if mode.strip() else ""
        node, next_at = await scheduler.set(
            name, schedule, prompt, services.session_key,
            message=message, mode=mode_slug,
            document_type=document_type.strip(),
        )
        await _note_mutation(services)
        when = (
            next_at.isoformat(sep=" ", timespec="minutes")
            if next_at is not None else "never"
        )
        if message.strip():
            kind = (
                "at fire time this message posts to the chat verbatim "
                "(no LLM turn)"
            )
        else:
            kind = (
                "at fire time an LLM turn runs the stored prompt in mode: "
                f"{mode_slug or '(space default)'}"
            )
            if document_type.strip():
                kind += (
                    f"; its output lands in a {document_type.strip()!r} "
                    "object (the chat gets a summary + link)"
                )
        return (
            f"scheduled {node.name!r} (id={node.id}); next fire: {when}; "
            f"{kind}. {_clock_line(scheduler)}. Verify the next-fire time "
            "matches what the user asked for; reschedule with "
            "action='cancel' + 'set' if not."
        )
    if action == "list":
        views = scheduler.events()
        if not views:
            return (
                "no scheduled events. Create one with action='set' "
                "(name, schedule, and prompt or message). "
                f"{_clock_line(scheduler)}."
            )
        lines = [_clock_line(scheduler), f"scheduled events ({len(views)}):"]
        for view in views:
            target = view.session_key or "(default chat)"
            mode_note = f", mode={view.mode}" if view.mode else ""
            doc_note = (
                f", document={view.document_type}" if view.document_type
                else ""
            )
            lines.append(
                f"- {view.node.name} (id={view.node.id}, chat={target}"
                f"{mode_note}{doc_note}) -- {view.status}"
            )
            if view.message.strip():
                lines.append(f"  message: {_schedule_excerpt(view.message)}")
                if view.prompt.strip():
                    lines.append(
                        "  (both Schedule message and Schedule prompt are "
                        "set; the message wins -- clear one)"
                    )
            else:
                excerpt = _schedule_excerpt(view.prompt)
                lines.append(
                    f"  prompt: {excerpt or '(none: fires with the name)'}"
                )
        return "\n".join(lines)
    if action == "cancel":
        node = await scheduler.cancel(node_id or name)
        await _note_mutation(services)
        return (
            f"cancelled {node.name!r} (id={node.id}); it will not fire. "
            "The object stays in Anytype with Schedule status 'Cancelled' "
            "-- the user can re-enable it there by setting the status back "
            "to Pending."
        )
    raise GraphContextError(
        f"unknown action {action!r}; allowed: set, list, cancel"
    )


async def _script_auto_test(services: Services, node: Node) -> str:
    """A just-saved 'run script' rule is dry-run immediately, so the
    authoring turn sees a script failure NOW instead of on the first
    live fire (built-in actions are fully validated at bind time, so
    only scripts need this). The rule is saved either way: a failing
    auto-test reports, it never rolls back."""
    if rules.parse_rule_fields(node.fields).action != rules.ACTION_RUN_SCRIPT:
        return ""
    try:
        report = await services.rules.dry_run(identifier=node.id)
    except GraphContextError as err:
        return (
            f"\nauto-test FAILED: {err}\nThe rule IS saved; if that is a "
            "script error, fix it with action='update' (script=...) "
            "before it fires live, then re-test with action='test'."
        )
    return f"\nauto-test of the saved script:\n{report}"


@guarded
async def automation_tool(
    services: Services,
    action: str = "list",
    name: str = "",
    rule: str = "",
    target_type: str = "",
    watch_property: str = "",
    condition: str = "",
    rule_action: str = "",
    action_property: str = "",
    action_value: str = "",
    script: str = "",
    trigger: str = "",
) -> str:
    engine = services.rules
    if action == "create":
        node = await engine.create(
            name, target_type, watch_property, condition, rule_action,
            action_property=action_property, action_value=action_value,
            script=script,
        )
        await _note_mutation(services)
        return (
            f"created automation rule {node.name!r} (id={node.id}). It "
            "runs on its own a few seconds after a matching change, "
            "while the assistant is serving this space. Check on it "
            "with action='list'; simulate it with action='test'."
            + await _script_auto_test(services, node)
        )
    if action == "update":
        node = await engine.update(
            rule or name,
            target_type=target_type, watch_property=watch_property,
            condition=condition, action=rule_action,
            action_property=action_property, action_value=action_value,
            script=script if script else None,
        )
        await _note_mutation(services)
        return (
            f"updated automation rule {node.name!r} (id={node.id}). "
            "Simulate it with action='test' to confirm the new behavior."
            + await _script_auto_test(services, node)
        )
    if action == "list":
        views = engine.views()
        if not views:
            return (
                "no automation rules. Create one with action='create' "
                "(name, target_type, watch_property, condition, "
                "rule_action)."
            )
        lines = [f"automation rules ({len(views)}):"]
        for view in views:
            lines.append(f"- {view.node.name} (id={view.node.id}) -- {view.status}")
            lines.append(f"  {view.summary}")
        return "\n".join(lines)
    if action in ("pause", "resume"):
        node = await engine.set_paused(rule or name, paused=action == "pause")
        await _note_mutation(services)
        if action == "pause":
            return (
                f"paused {node.name!r} (id={node.id}); it will not fire. "
                "Resume with action='resume', or in Anytype by setting "
                "its 'Rule status' back to Active."
            )
        return f"resumed {node.name!r} (id={node.id}); it is active again."
    if action == "test":
        return await engine.dry_run(
            identifier=rule, trigger=trigger,
            target_type=target_type, watch_property=watch_property,
            condition=condition, action=rule_action,
            action_property=action_property, action_value=action_value,
            script=script,
        )
    raise GraphContextError(
        f"unknown action {action!r}; allowed: create, update, list, "
        "pause, resume, test"
    )


# WP33 (ADR 041): the schema tool -- the LLM drafts a type change, the
# user confirms, only then does apply touch the space.


def _parse_property_drafts(
    properties: list[dict[str, Any]] | None,
) -> tuple[PropertyDraft, ...]:
    """Property entries as the tool surface takes them:
    ``{"name": ..., "format": ..., "options": [...]}`` (options may also
    arrive as one comma-separated string). Domain validation runs inside
    ``PropertyDraft``; here we only reshape and reject non-dict entries."""
    drafts: list[PropertyDraft] = []
    for entry in properties or []:
        if not isinstance(entry, dict):
            raise GraphContextError(
                "each properties entry must be an object like "
                "{'name': 'Status', 'format': 'select', "
                "'options': ['Open', 'Done']}"
            )
        options = entry.get("options") or ()
        if isinstance(options, str):
            options = [o.strip() for o in options.split(",") if o.strip()]
        drafts.append(PropertyDraft(
            name=str(entry.get("name", "")),
            format=str(entry.get("format", "")).strip().lower(),
            options=tuple(str(o) for o in options),
        ))
    return tuple(drafts)


def _render_proposal(proposal: SchemaProposal) -> str:
    lines = [f"proposal {proposal.id} (drafted, NOT applied):"]
    lines.extend(proposal.summary())
    lines.append(
        "A confirmation message will be posted right after your reply; "
        "the change applies ONLY if the user reacts \N{THUMBS UP SIGN} "
        "on it (\N{THUMBS DOWN SIGN} dismisses). You cannot apply it "
        "yourself -- do not claim the change is made, and do not repeat "
        "the proposal's contents in your reply (the confirmation message "
        "carries them). If the user asks for changes, cancel and "
        "re-propose."
    )
    return "\n".join(lines)


@guarded
async def schema_tool(
    services: Services,
    action: str = "list",
    type: str = "",
    plural: str = "",
    properties: list[dict[str, Any]] | None = None,
    reason: str = "",
    proposal_id: str = "",
) -> str:
    proposals = services.proposals
    if action == "propose_type":
        proposal = proposals.propose_type(
            services.repository, type, plural=plural,
            properties=_parse_property_drafts(properties), reason=reason,
        )
        return _render_proposal(proposal)
    if action == "propose_fields":
        proposal = proposals.propose_fields(
            services.repository, type,
            _parse_property_drafts(properties), reason=reason,
        )
        return _render_proposal(proposal)
    if action == "list":
        pending = proposals.pending()
        if not pending:
            return (
                "no pending schema proposals. Draft one with "
                "action='propose_type' (type, plural, properties) or "
                "action='propose_fields' (type, properties)."
            )
        lines = [f"pending schema proposals ({len(pending)}):"]
        for proposal in pending:
            lines.append(f"[{proposal.id}]")
            lines.extend(proposal.summary())
        lines.append(
            "Each applies only when the user reacts \N{THUMBS UP SIGN} "
            "on its confirmation message; you cannot apply them."
        )
        return "\n".join(lines)
    if action == "apply":
        # ADR 041 v2: apply is DELIBERATELY not a model action -- the
        # guarantee is that only a human's reaction executes a change.
        raise GraphContextError(
            "apply is not a model action: the change executes only when "
            "the USER reacts \N{THUMBS UP SIGN} on the proposal's "
            "confirmation message in the chat. If they agreed in words, "
            "point them at the confirmation message"
        )
    if action == "cancel":
        proposal = proposals.cancel(proposal_id)
        return (
            f"cancelled proposal {proposal.id} ({proposal.type_name!r}); "
            "nothing was changed. Re-propose any time."
        )
    raise GraphContextError(
        f"unknown action {action!r}; allowed: propose_type, "
        "propose_fields, list, cancel"
    )


# WP23 (ADR 032): send_file queues into the turn-scoped outbox; the
# transport does the actual upload after the reply is composed.
MAX_OUTBOUND_FILE_CHARS = 200_000
MAX_OUTBOUND_FILES_PER_TURN = 4


@guarded
async def send_file_tool(
    services: Services,
    name: str,
    content: str,
) -> str:
    filename = name.strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not filename:
        raise GraphContextError(
            "name must be a filename like 'report.md' or 'data.csv'"
        )
    if "." not in filename:
        raise GraphContextError(
            f"name {filename!r} needs an extension (e.g. {filename}.md) so "
            "the chat knows what kind of file it is"
        )
    if not content:
        raise GraphContextError(
            "content is empty -- pass the file's complete text"
        )
    if len(content) > MAX_OUTBOUND_FILE_CHARS:
        raise GraphContextError(
            f"content is {len(content)} characters; the cap is "
            f"{MAX_OUTBOUND_FILE_CHARS}. Split it into smaller files."
        )
    if len(services.outbox) >= MAX_OUTBOUND_FILES_PER_TURN:
        raise GraphContextError(
            f"already {MAX_OUTBOUND_FILES_PER_TURN} files queued this turn "
            "-- deliver these first and send more next turn"
        )
    services.outbox.append(OutboundFile(name=filename, content=content))
    return (
        f"queued {filename!r} ({len(content)} characters); it will be "
        "attached to your reply when the turn ends. Do NOT repeat the "
        "file's content in your reply text."
    )


_RETIRED_WRITE_PARAMS = {
    "fields": "pass the values inside properties={...}",
    "links": "pass relation values inside properties={'<relation>': "
             "'<node id or name>'}",
    "add_links": "pass relation values inside properties={'<relation>': "
                 "'<node id or name>'} (they ADD to existing links)",
    "create_missing_relations": "declare the label in "
        "create_missing_properties={'<label>': {'format': 'objects', "
        "'scope': 'instance'|'type'}}",
    "create_missing_fields": "declare the key in "
        "create_missing_properties={'<key>': '<format>'} (or "
        "{'format': ..., 'scope': 'instance'|'type'})",
}


def _reject_retired_params(supplied: dict[str, Any]) -> None:
    """ADR 042 replaced the fields/links surface; a replayed transcript
    or an old habit gets a self-correcting redirect, never an opaque
    internal error."""
    used = {k: v for k, v in supplied.items() if v is not None}
    if not used:
        return
    notes = "; ".join(
        f"'{key}' was replaced -- {_RETIRED_WRITE_PARAMS[key]}" for key in used
    )
    raise GraphContextError(notes)


@guarded
async def create_node_tool(
    services: Services,
    type: str,
    name: str,
    summary: str,
    description: str = "",
    story_time: float | str | None = None,
    properties: dict[str, Any] | None = None,
    icon: str = "",
    create_missing_properties: dict[str, Any] | None = None,
    # Retired params (ADR 042): explicit so an old-shape call gets a
    # redirect instead of guarded's opaque internal error.
    fields: dict[str, Any] | None = None,
    links: list[dict[str, Any]] | None = None,
    create_missing_relations: bool | None = None,
    create_missing_fields: dict[str, str] | None = None,
) -> str:
    _reject_retired_params({
        "fields": fields, "links": links,
        "create_missing_relations": create_missing_relations,
        "create_missing_fields": create_missing_fields,
    })
    scalars, parsed_links, declarations = await _parse_properties(
        services, properties, create_missing_properties,
        on_type=_parse_node_type(type),
    )
    draft = NodeDraft(
        type=_parse_node_type(type),
        name=name,
        summary=summary,
        # Tool-surface "description" = the node's body (ADR 010).
        body=description,
        story_time=story_time,
        fields=scalars,
        icon=icon.strip(),
    )
    outcome = await services.writer.create_node(
        draft, parsed_links, declarations=declarations,
        admitted_infra_roles=services.visible_infra_roles,
    )
    await _note_mutation(services)
    view = await services.reader.get_node(outcome.node.id)
    return (
        f"created:\n{presenters.render_node_view(view)}"
        f"{_render_write_outcome_notes(outcome)}"
    )


@guarded
async def update_node_tool(
    services: Services,
    node_id: str,
    name: str | None = None,
    summary: str | None = None,
    description: str | None = None,
    story_time: float | str | None = None,
    properties: dict[str, Any] | None = None,
    remove_links: list[dict[str, Any]] | None = None,
    create_missing_properties: dict[str, Any] | None = None,
    # Retired params (ADR 042): explicit so an old-shape call gets a
    # redirect instead of guarded's opaque internal error.
    fields: dict[str, Any] | None = None,
    add_links: list[dict[str, Any]] | None = None,
    create_missing_relations: bool | None = None,
    create_missing_fields: dict[str, str] | None = None,
) -> str:
    _reject_retired_params({
        "fields": fields, "add_links": add_links,
        "create_missing_relations": create_missing_relations,
        "create_missing_fields": create_missing_fields,
    })
    node_id = await _resolve(services, node_id)
    removals = [
        Edge(
            source=await _resolve(services, str(i["source"])),
            type=_parse_edge_type(str(i["edge_type"])),
            target=await _resolve(services, str(i["target"])),
            property_key=str(i.get("property_key", "")),
        )
        for i in remove_links or []
    ]
    scalars, parsed_add_links, declarations = await _parse_properties(
        services, properties, create_missing_properties, on_node=node_id,
    )
    outcome = await services.writer.update_node(
        node_id,
        name=name,
        summary=summary,
        description=description,
        story_time=story_time,
        fields=scalars if properties is not None else None,
        add_links=parsed_add_links,
        remove_links=removals,
        declarations=declarations,
        admitted_infra_roles=services.visible_infra_roles,
    )
    await _note_mutation(services)
    node = outcome.node
    stale_note = (
        "\nNOTE: summary flagged stale (no fresh summary in this update); "
        "supply `summary` to clear it."
        if node.summary_stale
        else ""
    )
    view = await services.reader.get_node(node.id)
    return (
        f"updated:\n{presenters.render_node_view(view)}{stale_note}"
        f"{_render_write_outcome_notes(outcome)}"
    )


_EDIT_DOCUMENT_ACTIONS = (
    "sections", "replace", "insert_after", "delete", "address_comment",
)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _render_sections(
    services: Services,
    node_id: NodeId,
    blocks: tuple[tuple[str, str], ...],
) -> str:
    """The document's anchor listing: one line per block -- hash, review
    state when a historian is live (WP42), first line of the text --
    plus the user's live comments (WP50)."""
    graph = services.repository.graph
    name = graph.node(node_id).name if graph.has_node(node_id) else node_id
    if not blocks:
        return f"{name} has no sections (empty body)."
    states = (
        services.historian.section_states(node_id)
        if services.historian is not None else {}
    )
    lines = [f"sections of {name} ({len(blocks)}):"]
    for block_hash, raw in blocks:
        state = states.get(block_hash)
        badge = f" · {state.status} · {state.intent}" if state else ""
        first_line = raw.splitlines()[0][:70]
        lines.append(f"[§{block_hash}{badge}] {first_line}")
    comments = (
        services.historian.comments(node_id)
        if services.historian is not None else ()
    )
    if comments:
        open_count = sum(
            1 for c in comments if c.state == revisions.COMMENT_OPEN
        )
        lines.append(
            f"comments ({open_count} open, "
            f"{len(comments) - open_count} addressed):"
        )
        for comment in comments:
            where = (
                f"on §{comment.hash}" if comment.hash
                else "(detached; its text was removed)"
            )
            lines.append(
                f'  #{comment.id} {comment.state} {where}: '
                f'"{_clip(comment.text, 100)}"'
            )
        lines.append(
            "address a comment you have acted on with "
            "action='address_comment' + comment_id; only the user "
            "resolves it."
        )
    return "\n".join(lines)


@guarded
async def edit_document_tool(
    services: Services,
    node_id: str,
    action: str = "sections",
    anchor: str = "",
    text: str = "",
    summary: str | None = None,
    comment_id: str = "",
) -> str:
    if action not in _EDIT_DOCUMENT_ACTIONS:
        raise GraphContextError(
            f"unknown action {action!r}; allowed: "
            f"{', '.join(_EDIT_DOCUMENT_ACTIONS)}."
        )
    resolved = await _resolve(services, node_id)
    editor = DocumentEditor(services.repository, services.writer)
    if action == "sections":
        return _render_sections(
            services, resolved, await editor.sections(resolved)
        )
    if action == "address_comment":
        # A sidecar bookkeeping write, not a body write: no journal
        # entry, no card -- the comment log keeps the record (WP50).
        historian = services.historian
        if historian is None:
            raise GraphContextError(
                "comments are unavailable on this surface (no revision "
                "history service)."
            )
        wanted = comment_id.strip().lstrip("#")
        if not wanted:
            raise GraphContextError(
                "action='address_comment' needs comment_id -- the #id "
                "shown in the sections listing and the context block."
            )
        await historian.set_comment_state(
            resolved, comment_id=wanted,
            value=revisions.COMMENT_ADDRESSED, by="model",
        )
        remaining = [
            c for c in historian.comments(resolved)
            if c.state == revisions.COMMENT_OPEN
        ]
        lines = [f"addressed comment #{wanted}."]
        if remaining:
            lines.append(f"still open ({len(remaining)}):")
            lines.extend(
                f'  #{c.id}: "{_clip(c.text, 100)}"' for c in remaining
            )
        else:
            lines.append("no comments remain open.")
        return "\n".join(lines)
    if action in ("replace", "insert_after") and not text.strip():
        raise GraphContextError(
            f"action={action!r} needs `text`: the section's full markdown "
            "(one or more paragraphs)."
        )
    outcome = await editor.edit(
        resolved, action=action, anchor=anchor, text=text, summary=summary,
        admitted_infra_roles=services.visible_infra_roles,
    )
    await _note_mutation(services)
    node = outcome.node
    stale_note = (
        "\nNOTE: summary flagged stale (no fresh summary in this update); "
        "supply `summary` to clear it."
        if node.summary_stale
        else ""
    )
    listing = _render_sections(
        services, resolved, await editor.sections(resolved)
    )
    return (
        f"edited {node.name} ({action}):\n{listing}{stale_note}"
        f"{_render_write_outcome_notes(outcome)}"
    )


def _render_write_outcome_notes(outcome: WriteOutcome) -> str:
    """The write's schema side-channel (ADR 042), after the node view:
    the auto-drafted type-attach proposal and any degraded warnings."""
    notes = []
    for proposal in outcome.drafted:
        notes.append(
            f"schema proposal {proposal.id} drafted (attach to "
            f"{proposal.type_name!r}): a confirmation message follows this "
            "reply -- the user applies it with a 👍 reaction; you cannot "
            "apply it. The value is already saved either way."
        )
    notes.extend(f"NOTE: {warning}" for warning in outcome.warnings)
    return "".join(f"\n{note}" for note in notes)


@guarded
async def get_node_tool(
    services: Services,
    node_id: str,
    edge_types: list[str] | None = None,
    include_provenance: int = 0,
) -> str:
    view = await services.reader.get_node(
        await _resolve(services, node_id),
        edge_type_filter=_edge_type_set(edge_types),
        include_provenance=include_provenance,
        excerpt_chars=presenters.EXCERPT_CHARS,
        visible_roles=services.visible_infra_roles,
    )
    return presenters.render_node_view(view)


@guarded
async def explore_tool(
    services: Services,
    start: str = "",
    depth: int = 1,
    include_types: list[str] | None = None,
    exclude_types: list[str] | None = None,
    edge_types: list[str] | None = None,
    as_of: float | str | None = None,
    include_future: bool = False,
    limit: int = 25,
    detail: str = "summaries",
    only_stale: bool = False,
) -> str:
    detail_level = _parse_detail(detail)  # fail fast, before any traversal
    excludes = _node_type_set(exclude_types) or frozenset()
    includes = _node_type_set(include_types)
    exclude_roles: frozenset[Role] = frozenset()
    if includes is None:
        # WP2 default: bookkeeping roles stay invisible unless included.
        exclude_roles = DEFAULT_EXPLORE_EXCLUDE_ROLES
    # Empty start still defaults to the session default in the Explorer.
    if start:
        start = await _resolve(services, start)
    result = await services.explorer.explore(
        ExploreQuery(
            start=start,
            depth=depth,
            include_node_types=includes,
            exclude_node_types=excludes,
            edge_types=_edge_type_set(edge_types),
            as_of=as_of,
            include_future=include_future,
            limit=limit,
            exclude_roles=exclude_roles,
        )
    )
    if only_stale:
        # WP3 stale-summary workflow: tool-layer narrowing, no new tool.
        from dataclasses import replace

        result = replace(
            result,
            hits=tuple(h for h in result.hits if h.node.summary_stale or h.depth == 0),
        )
    bodies = None
    if detail_level is Detail.FULL:
        # detail='full' = summaries + full bodies, fetched on demand
        # (ADR 010) -- after narrowing, so only rendered hits cost a GET.
        bodies = await services.explorer.bodies_for(
            [hit.node.id for hit in result.hits]
        )
    return presenters.render_explore_result(result, detail_level, bodies)


@guarded
async def query_tool(
    services: Services,
    type: str = "",
    linked_to: str = "",
    edge_types: list[str] | None = None,
    where: list[dict[str, Any]] | None = None,
    order_by: list[str] | None = None,
    view: str = "",
    limit: int = 25,
    detail: str = "summaries",
) -> str:
    detail_level = _parse_detail(detail)  # fail fast, before any scanning
    if view.strip():
        # WP13/ADR 018: a saved Set view IS a server-defined type+where+
        # order_by -- combining them is ambiguous, so it is an error.
        if type or linked_to or edge_types or where or order_by:
            raise GraphContextError(
                "view cannot be combined with type/linked_to/edge_types/"
                "where/order_by -- the view already defines those; drop "
                "them or drop view"
            )
        saved, view_result = await services.querier.run_view(
            view, limit=limit, exclude_roles=schema.INFRA_ROLES
        )
        view_bodies = None
        if detail_level is Detail.FULL:
            view_bodies = await services.explorer.bodies_for(
                [node.id for node in view_result.hits]
            )
        rendered = presenters.render_query_result(
            view_result, detail_level, saved.query.order_by, view_bodies
        )
        return f"view {saved.full_name!r}:\n{rendered}"
    predicates = _parse_predicates(where)
    sort_keys = _parse_order_by(order_by)
    node_type = type.strip() or None
    # Corpus scans reach everything, so hide ALL bookkeeping roles (not
    # just explore's default set -- mode config objects included) unless
    # the type filter explicitly names an infra type (same escape hatch
    # as explore's include_types). The active mode's meta privilege
    # (ADR 045) re-admits its roles; Role.MODE keeps NO unprivileged
    # hatch -- mode objects are assistant config, reachable only with
    # meta-inspection.
    exclude_roles: frozenset[Role] = (
        schema.INFRA_ROLES - services.visible_infra_roles
    )
    if node_type is not None:
        role = _validate_query_type(services, node_type)
        if (
            role is Role.MODE
            and Role.MODE not in services.visible_infra_roles
        ):
            raise GraphContextError(
                "Activity Mode objects are assistant configuration and "
                "are not visible in this mode; a mode with "
                "meta-inspection (the Space Setup mode) can inspect them"
            )
        if role in schema.INFRA_ROLES:
            exclude_roles = frozenset()
    anchor = await _resolve(services, linked_to) if linked_to else None
    result = await services.querier.query(
        NodeQuery(
            node_type=node_type,
            linked_to=anchor,
            edge_types=_edge_type_set(edge_types),
            predicates=predicates,
            order_by=sort_keys,
            limit=limit,
            exclude_roles=exclude_roles,
        )
    )
    bodies = None
    if detail_level is Detail.FULL:
        bodies = await services.explorer.bodies_for(
            [node.id for node in result.hits]
        )
    return presenters.render_query_result(result, detail_level, sort_keys, bodies)


@guarded
async def find_path_tool(
    services: Services,
    target: str,
    start: str = "",
    edge_types: list[str] | None = None,
    max_length: int = 4,
) -> str:
    path = await services.explorer.find_path(
        await _resolve(services, start) if start else None,
        await _resolve(services, target),
        edge_types=_edge_type_set(edge_types),
        max_length=max_length,
    )
    return presenters.render_path(path)


@guarded
async def find_node_tool(
    services: Services,
    name: str,
    type: str = "",
    limit: int = 10,
) -> str:
    def by_name() -> list[Node]:
        return services.repository.graph.find_by_name(
            name, node_type=type or None, limit=limit,
            include_roles=services.visible_infra_roles,
        )

    matches = by_name()
    if not matches and await resync_out_of_band(services):
        # A miss may just mean the node was created in the Anytype UI
        # after the last sync -- exactly the moment a stale index mints
        # duplicates (find -> "no match" -> create).
        matches = by_name()
    if matches or services.ranker is None:
        return presenters.render_node_matches(matches)
    # Tier 3 (ADRs 014/016): no name matched -- treat the input as a
    # DESCRIPTION. Hits are labelled so the LLM knows it holds fuzzy
    # matches, and each carries its evidence.
    hits = await services.ranker.rank(name, limit=limit)
    if type:
        wanted = type.strip().lower()
        hits = [
            h for h in hits
            if wanted in {h.node.type.lower(), h.node.type_key.lower()}
        ]
    if not hits:
        return presenters.render_node_matches([])  # honest empty + guidance
    return (
        f"find_node: no name match; {len(hits)} semantic match(es) for "
        f"{name!r}:\n{presenters.render_ranked_hits(hits)}"
    )
