"""Use-case: detecting property transitions and firing Automation Rules
(WP31, ADR 039).

An Automation Rule is a ``gc_rule`` node (``domain/rules.py`` owns the
vocabulary) watching one scalar property on one object type. The engine
runs one :meth:`RuleEngine.run_tick` per poll (the ``rules`` listener of
the Anytype bot's ``_watch_changes`` tick, under the space's turn lock,
right after a resync), diffing a private in-memory baseline of watched
values against the current index:

* the FIRST tick only records the baseline — nothing fires on restart
  or sync replay, and transitions made while the engine was down are
  absorbed ("detect transitions, not states");
* fires are at-most-once per (rule, object, transition): the baseline
  advances whether or not the action write succeeds — a failed action
  lands in ``gc_rule_last_error`` and is not retried;
* the baseline is rebuilt from the POST-action index at the end of the
  tick, so the engine's own writes can never read as transitions: rules
  never trigger rules, and no cascade or loop is possible.

Like the Scheduler, writes go straight to the repository (rules are
infrastructure bookkeeping; actions are automation, not turn mutations
— no journal, no recent-trail pollution) and ``now`` is injectable
naive local time (the scheduler's clock convention).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, time, tzinfo
from typing import Any

from graph_context.application.mutation_journal import MutationJournal, NullJournal
from graph_context.domain import fields as domain_fields
from graph_context.domain import rules
from graph_context.domain.graph import Direction
from graph_context.domain.models import FieldSpec, Node, NodeDraft, NodeId
from graph_context.domain.schema import INFRA_ROLES, Role
from graph_context.errors import GraphContextError, NodeNotFound, SchemaViolation
from graph_context.ports.graph_repository import GraphRepository
from graph_context.ports.script_runner import ScriptOutcome, ScriptRunner

logger = logging.getLogger(__name__)

_ERROR_LIMIT = 300  # gc_rule_last_error cap: a field, not a traceback
# Script sandbox caps (WP32, ADR 040): writes one fire may queue, and
# the largest space the snapshot export will serialize per fire.
_SCRIPT_MAX_SETS = 20
_SCRIPT_MAX_NODES = 2000


def _local_now() -> datetime:
    # Naive local wall-clock time, the scheduler's convention; the
    # composition root injects local_clock(GC_TIMEZONE) instead.
    return datetime.now()


def _stamp(moment: datetime) -> str:
    return moment.isoformat(sep=" ", timespec="seconds")


def _local_midnight(moment: datetime, zone: tzinfo | None) -> str:
    """Local midnight of ``moment``'s date, WITH its UTC offset.

    Anytype stores a bare date as midnight UTC and clients render it in
    the viewer's zone -- so a bare date displays one day early anywhere
    west of Greenwich. The aware RFC 3339 form (the only timestamp shape
    the live server accepts, R2) pins the calendar date in every zone.
    ``zone`` is GC_TIMEZONE's; ``None`` resolves the system offset at
    stamp time (a long-lived process must track DST).
    """
    midnight = datetime.combine(moment.date(), time.min)
    aware = midnight.replace(tzinfo=zone) if zone else midnight.astimezone()
    return aware.isoformat()


def _truncate(message: str) -> str:
    text = " ".join(message.split())
    return text if len(text) <= _ERROR_LIMIT else text[: _ERROR_LIMIT - 1] + "…"


@dataclass(frozen=True, slots=True)
class RuleFiring:
    """One executed (rule, object) fire, for the watcher's log line."""

    rule_id: NodeId
    rule_name: str
    node_id: NodeId
    node_name: str
    action: str
    # ADR 058: a human asked for this fire (the run-now checkbox or the
    # /run command) rather than a transition causing it.
    manual: bool = False


@dataclass(frozen=True, slots=True)
class RuleProblem:
    """One NEWLY-recorded rule error (bad config or failed action)."""

    rule_id: NodeId
    rule_name: str
    message: str


@dataclass(frozen=True, slots=True)
class RuleTickReport:
    """One tick's outcome. ``errors`` lists only errors recorded THIS
    tick (bookkeeping writes are change-only, so this mirrors what was
    actually written); ``healed`` lists rules whose Error status was
    cleared after their config parsed again."""

    fired: tuple[RuleFiring, ...] = ()
    errors: tuple[RuleProblem, ...] = ()
    healed: tuple[NodeId, ...] = ()


def describe_firing(firing: RuleFiring) -> str:
    """One fire, in a line. Shared by the /run reply and the bot's rule
    log so a human sees the same sentence in both places (ADR 058)."""
    how = "ran" if firing.manual else "fired"
    return (
        f"{how} {firing.rule_name!r} on {firing.node_name!r} "
        f"({firing.action})"
    )


def describe_problem(problem: RuleProblem) -> str:
    return f"{problem.rule_name!r}: {problem.message}"


def describe_report(report: RuleTickReport) -> list[str]:
    """A tick or manual run as human-readable lines, most important
    first. Empty when nothing happened -- callers say so in their own
    words, since "no rules matched" and "nothing to do" differ."""
    lines = [describe_firing(f) for f in report.fired]
    lines.extend(describe_problem(p) for p in report.errors)
    return lines


@dataclass(frozen=True, slots=True)
class RuleView:
    """One rule as the ``automation`` tool's list action renders it."""

    node: Node
    status: str  # active / paused / error: ... (+ last fired)
    summary: str  # one-line config: when <watch> on <type> <cond> -> <action>


@dataclass(frozen=True, slots=True)
class _BoundRule:
    """A parsed rule resolved against the space: which fields key to
    read, which to write, and the write target's format ("" unknown).
    ``script`` is attached separately (async body fetch) for the run
    script action; "" until then."""

    node: Node
    config: rules.RuleConfig
    read_key: str
    action_key: str
    action_format: str
    script: str = ""
    # ADR 042: the watch resolved to a store-clock built-in
    # (rules.BUILTIN_WATCH_MODIFIED) -- read Node.modified_at, not fields.
    builtin_watch: bool = False

    @property
    def watches(self) -> bool:
        """Whether this rule has anything to diff (ADR 058): a manual
        rule may name no watch property, so it is baselined by nothing
        and planned by nothing."""
        return bool(self.builtin_watch or self.read_key.strip())


class RuleEngine:
    """Automation-rule reads and writes over the shared repository."""

    def __init__(
        self,
        repository: GraphRepository,
        now: Callable[[], datetime] = _local_now,
        zone: tzinfo | None = None,
        script_runner: ScriptRunner | None = None,
        journal: MutationJournal | None = None,
    ) -> None:
        self._repository = repository
        self._now = now
        # The zone behind `now` (GC_TIMEZONE); date stamps need the
        # explicit UTC offset the naive clock strips. None = system.
        self._zone = zone
        self._script_runner = script_runner
        # The journal covers the TOOL's writes (intent provenance, like
        # the scheduler); the tick's automation writes stay unjournalled.
        self._journal = journal or NullJournal()
        # object id -> {watched fields key -> last-seen value}. None =
        # never baselined (first tick records, never fires).
        self._snapshot: dict[NodeId, dict[str, str]] | None = None
        self._noted_overlaps: set[tuple[NodeId, NodeId]] = set()
        # rule id -> (modified_at stamp, extracted script): saves a body
        # GET per script rule per tick. A body edit bumps modified_at and
        # invalidates; the memory backend's "" stamp never hits (its
        # fetch_body is free, so refetch-every-tick is correct there).
        self._scripts: dict[NodeId, tuple[str, str]] = {}

    async def run_tick(self) -> RuleTickReport:
        """One diff-fire-rebaseline pass over the shared index.

        Two plan sources feed one execution: transitions since the last
        baseline, and the manual runs humans requested by ticking
        ``gc_rule_run_now`` (ADR 058). They merge here rather than in a
        second listener so a rule that both transitioned and was
        hand-requested still books and rebaselines exactly once.
        """
        bound, broken = self._load_rules()
        bound, broken = await self._attach_scripts(bound, broken)
        self._note_overlaps(bound)
        requested, refused = await self._claim_manual_requests(bound, broken)
        plans = self._plan(bound, self._snapshot) + requested
        fired, failures = await self._apply(plans)
        report = await self._write_bookkeeping(bound, broken, fired, failures)
        if refused:
            report = replace(report, errors=report.errors + tuple(refused))
        self._rebaseline(bound)
        return report

    async def _apply(
        self, plans: list[tuple[_BoundRule, Node, str, str]]
    ) -> tuple[list[RuleFiring], dict[NodeId, str]]:
        """Execute planned fires, folding action failures into per-rule
        messages. A failed action is recorded, never retried -- the
        transition (or the manual request) is consumed either way."""
        fired: list[RuleFiring] = []
        failures: dict[NodeId, str] = {}
        for rule, obj, before, after in plans:
            try:
                await self._execute(rule, obj, before, after)
            except GraphContextError as err:
                failures[rule.node.id] = _truncate(str(err))
                continue
            fired.append(RuleFiring(
                rule_id=rule.node.id,
                rule_name=rule.node.name,
                node_id=obj.id,
                node_name=obj.name,
                action=rule.config.action,
                manual=rule.config.condition == rules.CONDITION_MANUAL,
            ))
        return fired, failures

    def _rebaseline(self, bound: list[_BoundRule]) -> None:
        """Rebuild the baseline from the POST-action index over EVERY
        bound rule -- ADR 039's load-bearing invariant. The engine's own
        writes are folded in before the next diff, so they can never
        read as transitions: rules never trigger rules, and no cascade
        or loop is possible. Manual runs (ADR 058) obey it too, which is
        why they rebaseline the full set and not just the rule that ran.
        """
        self._snapshot = self._collect(bound)

    # -- the automation tool's backend (WP32, ADR 040) ---------------------

    async def create(
        self,
        name: str,
        target_type: str,
        watch_property: str = "",  # optional for a manual rule (ADR 058)
        condition: str = "",
        action: str = "",
        action_property: str = "",
        action_value: str = "",
        script: str = "",
    ) -> Node:
        """Create an Automation Rule node, validated up front.

        Config errors (unknown action word, missing property, a property
        the type does not have) surface HERE, at the tool boundary --
        not as an Error status on the next tick. A ``run script`` rule
        requires ``script``; it is stored as the node body's fenced
        ```python block, exactly where a human would author it.
        """
        if not name.strip():
            raise GraphContextError("an automation rule needs a non-empty 'name'")
        fields = self._rule_fields(
            target_type=target_type, watch_property=watch_property,
            condition=condition, action=action,
            action_property=action_property, action_value=action_value,
        )
        fields[rules.FIELD_STATUS] = rules.STATUS_ACTIVE
        config = rules.parse_rule_fields(fields)
        self._probe_bind(name.strip(), config)
        body = ""
        if config.action == rules.ACTION_RUN_SCRIPT:
            if not script.strip():
                raise GraphContextError(
                    "action 'run script' needs 'script': the Python "
                    "source to run when the condition fires"
                )
            if self._script_runner is None:
                raise GraphContextError(
                    "the run script action is not available in this "
                    "deployment (no script runner configured)"
                )
            body = f"```python\n{script.strip()}\n```"
        node = await self._repository.create_node(NodeDraft(
            type=rules.RULE_TYPE_KEY,
            name=name.strip(),
            summary=f"automation: {self._config_line(config)}",
            fields=fields,
            body=body,
            icon="⚡",
        ))
        self._journal.created(node.id)
        return node

    async def update(
        self,
        identifier: str,
        *,
        target_type: str = "",
        watch_property: str = "",
        condition: str = "",
        action: str = "",
        action_property: str = "",
        action_value: str = "",
        script: str | None = None,
    ) -> Node:
        """Update a rule's config and/or script; empty params keep the
        stored value (clearing a field is a UI edit, not a tool call).
        The merged config is re-validated before anything is written."""
        node = self._find(identifier)
        merged = {**dict(node.fields), **self._rule_fields(
            target_type=target_type, watch_property=watch_property,
            condition=condition, action=action,
            action_property=action_property, action_value=action_value,
        )}
        config = rules.parse_rule_fields(merged)
        self._probe_bind(node.name, config)
        if script is not None and config.action != rules.ACTION_RUN_SCRIPT:
            raise GraphContextError(
                "'script' only applies to action 'run script'; set the "
                "action too if you meant to convert this rule"
            )
        body: str | None = None
        if script is not None:
            if not script.strip():
                raise GraphContextError(
                    "'script' must be non-empty Python source"
                )
            body = f"```python\n{script.strip()}\n```"
            self._scripts.pop(node.id, None)  # replaced: drop the cache
        await self._repository.update_node(node.id, fields=merged, body=body)
        self._journal.modified(node.id)
        return self._repository.graph.node(node.id)

    def views(self) -> list[RuleView]:
        """Every Automation Rule with a rendered status, name-sorted."""
        views = []
        for node in self._rule_nodes():
            views.append(RuleView(
                node=node,
                status=self._status_line(node),
                summary=self._summary_line(node),
            ))
        return sorted(views, key=lambda v: (v.node.name.casefold(), v.node.id))

    async def set_paused(self, identifier: str, paused: bool) -> Node:
        """Flip a rule's status to Paused / back to Active. The node and
        its config stay intact -- resume is one flip, like re-Pending'ing
        a Scheduled Event."""
        node = self._find(identifier)
        status = rules.STATUS_PAUSED if paused else rules.STATUS_ACTIVE
        changes = {rules.FIELD_STATUS: status}
        if not paused:
            changes[rules.FIELD_LAST_ERROR] = ""
        await self._write_rule_fields(node, changes)
        self._journal.modified(node.id)
        return self._repository.graph.node(node.id)

    # -- manual runs (WP52, ADR 058) ---------------------------------------

    async def run_now(
        self, identifier: str, trigger: str = ""
    ) -> RuleTickReport:
        """Fire ONE rule immediately, because a human asked -- the /run
        command's backend. No transition is required and the condition
        is not consulted: an explicit request IS the trigger.

        The trigger object is ``trigger`` (id or exact name among the
        target type's objects) or the first of that type, exactly as a
        dry run picks one. Everything else -- the sandbox, effect
        validation, bookkeeping, the rebaseline -- is the tick's own
        machinery, so a manual fire inherits its guarantees rather than
        re-implementing them.
        """
        node = self._find(identifier)
        if rules.is_paused(node.fields.get(rules.FIELD_STATUS, "")):
            # Paused means off, for every trigger. Raising (rather than
            # reporting) lets /run render it as an error and the
            # checkbox path record it -- one refusal, two consumers.
            raise GraphContextError(
                f"rule {node.name!r} is paused; resume it first "
                "(automation action='resume', or set 'Rule status' to "
                "Active on the rule object in Anytype)"
            )
        bound, broken = self._load_rules()
        bound, broken = await self._attach_scripts(bound, broken)
        rule = next((r for r in bound if r.node.id == node.id), None)
        if rule is None:
            raise GraphContextError(self._why_unrunnable(node, broken))
        obj = self._resolve_trigger(rule, trigger)
        before, after = self._synthesize_transition(rule, obj)
        fired, failures = await self._apply([(rule, obj, before, after)])
        # Bookkeeping is scoped to the ONE rule the user named, so /run
        # never rewrites another rule's error/heal state; the rebaseline
        # below is deliberately NOT -- see _rebaseline.
        report = await self._write_bookkeeping([rule], [], fired, failures)
        self._rebaseline(bound)
        return report

    def _why_unrunnable(
        self, node: Node, broken: list[tuple[Node, str]]
    ) -> str:
        """Why a rule that exists did not bind -- the stored parse or
        script-attach error, so /run explains itself the same way the
        rule object's own 'Rule last error' does."""
        for candidate, message in broken:
            if candidate.id == node.id:
                return f"rule {node.name!r} cannot run: {message}"
        # Not broken and not bound: the unconfigured template. Let the
        # domain say what is missing rather than guessing.
        try:
            rules.parse_rule_fields(node.fields)
        except GraphContextError as err:
            return f"rule {node.name!r} cannot run: {err}"
        return f"rule {node.name!r} cannot run: its configuration is incomplete"

    async def _claim_manual_requests(
        self,
        bound: list[_BoundRule],
        broken: list[tuple[Node, str]],
    ) -> tuple[list[tuple[_BoundRule, Node, str, str]], list[RuleProblem]]:
        """Turn ticked ``gc_rule_run_now`` boxes into planned fires.

        Every rule node is scanned, not just the bound ones -- a human
        can tick the box on a paused, broken, or half-configured rule,
        and each deserves an answer in ``gc_rule_last_error``.

        The box is UNTICKED BEFORE the rule runs, the scheduler's
        mark-fired-before-firing discipline: the request is claimed at
        most once, so a crashing action cannot re-fire forever. A failed
        claim leaves the box ticked and skips the fire, which is why
        this cannot go through the error-swallowing
        ``_write_rule_fields``.
        """
        by_id = {rule.node.id: rule for rule in bound}
        plans: list[tuple[_BoundRule, Node, str, str]] = []
        refused: list[RuleProblem] = []
        for node in sorted(
            self._rule_nodes(), key=lambda n: (n.name.casefold(), n.id)
        ):
            requested = self._field_of(node.fields, rules.FIELD_RUN_NOW)
            if requested.strip().lower() != "true":
                continue
            if not await self._release_request(node):
                continue
            paused = rules.is_paused(node.fields.get(rules.FIELD_STATUS, ""))
            rule = by_id.get(node.id)
            if paused:
                await self._refuse(node, refused, (
                    f"rule {node.name!r} is paused; resume it before "
                    "running it by hand"
                ), touch_status=False)
                continue
            if rule is None:
                await self._refuse(
                    node, refused, self._why_unrunnable(node, broken),
                )
                continue
            try:
                obj = self._resolve_trigger(rule, "")
            except GraphContextError as err:
                await self._refuse(node, refused, str(err))
                continue
            before, after = self._synthesize_transition(rule, obj)
            plans.append((rule, obj, before, after))
        return plans, refused

    async def _release_request(self, node: Node) -> bool:
        """Untick one run-now box, claiming the request. False when the
        write failed -- the box stays ticked and the next tick retries,
        which is safer than firing on a request we could not consume."""
        try:
            fresh = self._repository.graph.node(node.id)
            merged = {
                **dict(fresh.fields), rules.FIELD_RUN_NOW: "false",
            }
            await self._repository.update_node(fresh.id, fields=merged)
        except GraphContextError as err:
            logger.warning(
                "could not claim the manual run of rule %s: %s", node.id, err
            )
            return False
        return True

    async def _refuse(
        self,
        node: Node,
        refused: list[RuleProblem],
        message: str,
        touch_status: bool = True,
    ) -> None:
        """Record why a claimed manual request could not run.

        ``touch_status`` is False for a PAUSED rule: writing Error there
        would make ``is_paused`` false, the rule would be scanned next
        tick and self-heal to Active -- so ticking a box would silently
        resume a rule the human switched off.
        """
        text = _truncate(message)
        changes = {rules.FIELD_LAST_ERROR: text}
        if touch_status and node.fields.get(
            rules.FIELD_STATUS, ""
        ).strip() != rules.STATUS_ERROR:
            changes[rules.FIELD_STATUS] = rules.STATUS_ERROR
        await self._write_rule_fields(node, changes)
        refused.append(RuleProblem(
            rule_id=node.id, rule_name=node.name, message=text,
        ))

    async def dry_run(
        self,
        identifier: str = "",
        trigger: str = "",
        *,
        target_type: str = "",
        watch_property: str = "",
        condition: str = "",
        action: str = "",
        action_property: str = "",
        action_value: str = "",
        script: str = "",
    ) -> str:
        """The tool's test action: simulate one fire, apply NOTHING.

        Pass ``identifier`` to test a stored rule -- with ``script`` as
        an inline REPLACEMENT run against the stored config, so a fix
        can be iterated without saving every attempt -- or the config
        params (+ ``script``) to test a DRAFT before creating it. The
        trigger object is ``trigger`` (id or name) or the first object
        of the target type. The transition is synthesized to satisfy
        the condition; a script's queued writes are validated with the
        same code path a real fire uses and reported as would-write
        lines.
        """
        if identifier.strip():
            node = self._find(identifier)
            fields = dict(node.fields)
            config = rules.parse_rule_fields(fields)
            bound = self._bind(
                node, config, self._repository.field_catalog()
            )
            if config.action == rules.ACTION_RUN_SCRIPT:
                bound = replace(
                    bound,
                    script=script.strip() or await self._script_for(node),
                )
            elif script.strip():
                raise GraphContextError(
                    f"rule {node.name!r} runs {config.action!r}, not a "
                    "script -- 'script' only applies to 'run script' "
                    "rules; drop it, or pass the draft config params "
                    "instead of 'rule'"
                )
        else:
            fields = self._rule_fields(
                target_type=target_type, watch_property=watch_property,
                condition=condition, action=action,
                action_property=action_property, action_value=action_value,
            )
            config = rules.parse_rule_fields(fields)
            probe = self._probe_bind("(draft)", config)
            bound = replace(probe, script=script.strip())
            if config.action == rules.ACTION_RUN_SCRIPT and not bound.script:
                raise GraphContextError(
                    "testing a 'run script' draft needs 'script'"
                )
        if (
            bound.config.action == rules.ACTION_RUN_SCRIPT
            and self._script_runner is None
        ):
            raise GraphContextError(
                "the run script action is not available in this "
                "deployment (no script runner configured)"
            )
        obj = self._resolve_trigger(bound, trigger)
        before, after = self._synthesize_transition(bound, obj)
        lines = [
            f"dry run against {obj.name!r} ({obj.id}); simulating "
            f"{bound.config.condition!r}: before={before!r} after={after!r}",
        ]
        lines.extend(await self._dry_run_effects(bound, obj, before, after))
        lines.append("nothing was applied (dry run)")
        return "\n".join(lines)

    def _rule_fields(self, **params: str) -> dict[str, str]:
        """Tool params -> stored fields, selects in their seeded display
        form (so writes REUSE the seeded option tags instead of minting
        lowercase duplicates)."""
        fields: dict[str, str] = {}
        keys = {
            "target_type": rules.FIELD_TARGET_TYPE,
            "watch_property": rules.FIELD_WATCH_PROPERTY,
            "condition": rules.FIELD_CONDITION,
            "action": rules.FIELD_ACTION,
            "action_property": rules.FIELD_ACTION_PROPERTY,
            "action_value": rules.FIELD_ACTION_VALUE,
        }
        for param, key in keys.items():
            value = params.get(param, "").strip()
            if not value:
                continue
            if key in (rules.FIELD_CONDITION, rules.FIELD_ACTION):
                value = rules.normalize_choice(value).capitalize()
            fields[key] = value
        return fields

    def _probe_bind(self, name: str, config: rules.RuleConfig) -> _BoundRule:
        """Bind-time validation without a persisted node: catches
        unknown-property config at the tool boundary."""
        probe = Node(
            id="", type="Automation Rule", name=name, summary="",
            role=Role.RULE,
        )
        return self._bind(probe, config, self._repository.field_catalog())

    def _find(self, identifier: str) -> Node:
        """Resolve an id or an exact name AMONG automation rules (shared
        name resolution excludes infra roles, so like the scheduler the
        tool does its own)."""
        wanted = identifier.strip()
        if not wanted:
            raise GraphContextError(
                "pass 'rule': an automation rule's id or exact name "
                "(action='list' shows both)"
            )
        graph = self._repository.graph
        if graph.has_node(wanted):
            node = graph.node(wanted)
            if node.role is not Role.RULE:
                raise GraphContextError(
                    f"{node.name!r} ({node.type}) is not an Automation Rule"
                )
            return node
        matches = [
            node for node in self._rule_nodes()
            if node.name.strip().casefold() == wanted.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise NodeNotFound(identifier)
        listing = "; ".join(f"{n.name} (id={n.id})" for n in matches)
        raise GraphContextError(
            f"{identifier!r} names {len(matches)} automation rules: "
            f"{listing}. Retry with an exact id."
        )

    def _resolve_trigger(self, rule: _BoundRule, trigger: str) -> Node:
        targets = sorted(self._targets(rule), key=lambda n: n.id)
        wanted = trigger.strip()
        if not wanted:
            if not targets:
                raise GraphContextError(
                    f"no objects of type {rule.config.target_type!r} exist "
                    "to test against; pass 'trigger' or create one first"
                )
            return targets[0]
        graph = self._repository.graph
        if graph.has_node(wanted):
            return graph.node(wanted)
        matches = [
            node for node in targets
            if node.name.strip().casefold() == wanted.casefold()
        ]
        if len(matches) == 1:
            return matches[0]
        raise GraphContextError(
            f"trigger {trigger!r} matches no object of type "
            f"{rule.config.target_type!r} (pass an id or exact name)"
        )

    def _synthesize_transition(
        self, rule: _BoundRule, obj: Node
    ) -> tuple[str, str]:
        """The (before, after) a fire runs under. For every automatic
        condition this is a SYNTHESIS -- the transition the condition
        describes, for a dry run that never happened. For ``manual``
        it is the plain truth: no transition occurred, so the script
        sees the current value on both sides. That is what lets a real
        manual fire and a dry run share this whole prelude."""
        if rule.config.condition == rules.CONDITION_MANUAL:
            current = self._watched_value(rule, obj) if rule.watches else ""
            return current, current
        if rule.builtin_watch:
            # Two distinct stamps: any edit bumps the store clock.
            return "(an earlier stamp)", obj.modified_at or _stamp(self._now())
        current = self._field_of(obj.fields, rule.read_key)
        if rule.config.condition == rules.CONDITION_CHANGED_TO_TRUE:
            return "", "true"
        if rule.config.condition == rules.CONDITION_CHANGED_TO_FALSE:
            return "true", ""
        return "", current or "(new value)"

    async def _dry_run_effects(
        self, rule: _BoundRule, obj: Node, before: str, after: str
    ) -> list[str]:
        config = rule.config
        if config.action == rules.ACTION_SET_NOW:
            value = self._now_value(rule.action_format)
            return [f"would set {obj.name!r}.{config.action_property} = {value!r}"]
        if config.action == rules.ACTION_SET_VALUE:
            return [
                f"would set {obj.name!r}.{config.action_property} = "
                f"{config.action_value!r}"
            ]
        if config.action == rules.ACTION_UNCHECK_OTHERS:
            unchecked = [
                f"would set {sibling.name!r}.{config.action_property} = 'false'"
                for sibling in sorted(self._targets(rule), key=lambda n: n.id)
                if sibling.id != obj.id
                and self._field_of(
                    sibling.fields, rule.action_key
                ).strip().lower() == "true"
            ]
            return unchecked or ["no other object is currently checked: no writes"]
        assert self._script_runner is not None  # guarded by dry_run
        payload = self._script_payload(rule, obj, before, after)
        outcome = await self._script_runner.run(rule.script, payload)
        lines = [f"script log: {line}" for line in outcome.logs]
        try:
            planned = self._validated_effects(outcome)
        except GraphContextError as err:
            lines.append(f"validation failed (nothing would apply): {err}")
            return lines
        lines.extend(
            f"would set {target.name!r}.{key} = {value!r}"
            for target, key, value in planned
        )
        if not planned:
            lines.append("the script queued no writes")
        return lines

    def _status_line(self, node: Node) -> str:
        stored = node.fields.get(rules.FIELD_STATUS, "").strip()
        if rules.is_paused(stored):
            status = "paused"
        elif stored.lower() == rules.STATUS_ERROR.lower():
            error = node.fields.get(rules.FIELD_LAST_ERROR, "").strip()
            status = f"error: {error}" if error else "error"
        else:
            status = "active"
        fired = node.fields.get(rules.FIELD_LAST_FIRED, "").strip()
        return f"{status}; last fired {fired}" if fired else status

    def _summary_line(self, node: Node) -> str:
        if rules.is_unconfigured(node.fields):
            return "unconfigured template"
        try:
            return self._config_line(rules.parse_rule_fields(node.fields))
        except GraphContextError as err:
            return f"invalid: {err}"

    def _config_line(self, config: rules.RuleConfig) -> str:
        if config.condition == rules.CONDITION_MANUAL:
            # No watch to name: say what it DOES take to run it.
            line = f"manual run only on {config.target_type!r} -> {config.action}"
        else:
            line = (
                f"when {config.watch_property!r} on {config.target_type!r} "
                f"{config.condition} -> {config.action}"
            )
        if config.action in (rules.ACTION_SET_NOW, rules.ACTION_SET_VALUE):
            line += f" {config.action_property!r}"
        if config.action == rules.ACTION_SET_VALUE:
            line += f" = {config.action_value!r}"
        return line

    # -- rule loading ------------------------------------------------------

    def _load_rules(
        self,
    ) -> tuple[list[_BoundRule], list[tuple[Node, str]]]:
        """Parse + resolve every scannable rule node, in a stable order.
        Returns (bound rules, (node, error) pairs for broken configs)."""
        catalog = self._repository.field_catalog()
        bound: list[_BoundRule] = []
        broken: list[tuple[Node, str]] = []
        for node in sorted(
            self._rule_nodes(), key=lambda n: (n.name.casefold(), n.id)
        ):
            if rules.is_paused(node.fields.get(rules.FIELD_STATUS, "")):
                continue
            if rules.is_unconfigured(node.fields):
                continue  # a template/explainer, not a mistake
            try:
                config = rules.parse_rule_fields(node.fields)
                bound.append(self._bind(node, config, catalog))
            except GraphContextError as err:
                broken.append((node, _truncate(str(err))))
        return bound, broken

    def _rule_nodes(self) -> list[Node]:
        return [
            node for node in self._repository.graph.nodes()
            if node.role is Role.RULE
        ]

    async def _attach_scripts(
        self,
        bound: list[_BoundRule],
        broken: list[tuple[Node, str]],
    ) -> tuple[list[_BoundRule], list[tuple[Node, str]]]:
        """Fetch each script rule's source from its node body (WP32).

        A rule without a usable script is DEMOTED to the broken list --
        it rides the same Error/last-error/self-heal bookkeeping as a
        bad config, and healing is free: fixing the body bumps
        ``modified_at``, which invalidates the cache entry.
        """
        attached: list[_BoundRule] = []
        seen: set[NodeId] = set()
        for rule in bound:
            if rule.config.action != rules.ACTION_RUN_SCRIPT:
                attached.append(rule)
                continue
            seen.add(rule.node.id)
            if self._script_runner is None:
                broken.append((rule.node, _truncate(
                    "the run script action is not available in this "
                    "deployment (no script runner configured)"
                )))
                continue
            try:
                script = await self._script_for(rule.node)
            except GraphContextError as err:
                broken.append((rule.node, _truncate(str(err))))
                continue
            attached.append(replace(rule, script=script))
        for stale in set(self._scripts) - seen:
            del self._scripts[stale]
        return attached, broken

    async def _script_for(self, node: Node) -> str:
        cached = self._scripts.get(node.id)
        if cached is not None and node.modified_at and cached[0] == node.modified_at:
            return cached[1]
        body = await self._repository.fetch_body(node.id)
        script = rules.extract_script(body)
        if not script:
            self._scripts.pop(node.id, None)
            raise GraphContextError(
                "the rule's body needs a ```python fenced code block -- "
                "it runs when the condition fires"
            )
        self._scripts[node.id] = (node.modified_at, script)
        return script

    def _bind(
        self,
        node: Node,
        config: rules.RuleConfig,
        catalog: Mapping[str, tuple[FieldSpec, ...]],
    ) -> _BoundRule:
        """Resolve the rule's property names against the space catalog.

        A type the catalog does not know degrades to literal
        (case-insensitive) fields-key matching instead of erroring — the
        memory backend has no space schema, and a type with no scalar
        properties yet should not brand the rule broken. A type the
        catalog DOES know validates loudly, echoing the type's actual
        properties.
        """
        specs = self._type_specs(catalog, config.target_type)
        read_key, read_format, builtin_watch = self._resolve_watch(
            specs, config
        )
        if config.action == rules.ACTION_RUN_SCRIPT:
            # No fixed write target: the script's own set() calls are
            # validated per effect at execution time.
            action_key, action_format = "", ""
        else:
            if (
                rules.builtin_watch_for(config.action_property) is not None
                and self._spec_match(specs, config.action_property) is None
            ):
                raise SchemaViolation(
                    f"{config.action_property!r} is the store's own "
                    "last-modified stamp -- read-only, the store writes "
                    "it; it can be watched but never set"
                )
            action_key, action_format = self._resolve_property(
                specs, config.target_type, config.action_property
            )
        if config.action == rules.ACTION_UNCHECK_OTHERS:
            for label, fmt in (
                (config.watch_property, read_format),
                (config.action_property, action_format),
            ):
                if fmt and fmt != "checkbox":
                    raise SchemaViolation(
                        f"action {rules.ACTION_UNCHECK_OTHERS!r} works on "
                        f"checkbox properties; {label!r} is a {fmt} property"
                    )
        if config.action == rules.ACTION_SET_VALUE:
            # Fail at validation time, visibly on the rule object, not at
            # 3am on the first fire. Selects are exempt: options
            # auto-create on write (ADR 012).
            if action_format == "checkbox":
                domain_fields.parse_checkbox(
                    config.action_property, config.action_value
                )
            elif action_format == "number":
                domain_fields.parse_number(
                    config.action_property, config.action_value
                )
        return _BoundRule(
            node=node,
            config=config,
            read_key=read_key,
            action_key=action_key,
            action_format=action_format,
            builtin_watch=builtin_watch,
        )

    def _resolve_watch(
        self,
        specs: tuple[FieldSpec, ...] | None,
        config: rules.RuleConfig,
    ) -> tuple[str, str, bool]:
        """Resolve the watch property: the space catalog first (a space's
        own "Modified date" property wins), then the built-in store-clock
        watchables (ADR 042) -- available on EVERY type, condition
        'changed' only (a timestamp has no true/false).
        -> (read key, format or "", is-builtin)."""
        identifier = config.watch_property
        if not identifier.strip():
            # A manual rule watches nothing (ADR 058). Trusting
            # parse_rule_fields -- which is where "only manual may omit
            # it" lives -- keeps that rule in one place; without this,
            # resolving "" against a KNOWN catalog type raises, so the
            # config would break on every real space while passing on
            # the memory backend.
            return "", "", False
        builtin = rules.builtin_watch_for(identifier)
        if specs is not None and self._spec_match(specs, identifier) is not None:
            key, fmt = self._resolve_property(
                specs, config.target_type, identifier
            )
            return key, fmt, False
        if builtin is None:
            key, fmt = self._resolve_property(
                specs, config.target_type, identifier
            )
            return key, fmt, False
        if config.condition != rules.CONDITION_CHANGED:
            raise SchemaViolation(
                f"the built-in {rules.BUILTIN_WATCH_MODIFIED!r} watch only "
                f"supports condition {rules.CONDITION_CHANGED!r} -- a "
                "modified stamp has no true/false"
            )
        return rules.BUILTIN_WATCH_MODIFIED, "builtin", True

    @staticmethod
    def _spec_match(
        specs: tuple[FieldSpec, ...] | None, identifier: str
    ) -> FieldSpec | None:
        if specs is None:
            return None
        wanted = identifier.strip().lower()
        for spec in specs:
            if wanted in (spec.key.strip().lower(), spec.name.strip().lower()):
                return spec
        return None

    def _type_specs(
        self,
        catalog: Mapping[str, tuple[FieldSpec, ...]],
        target_type: str,
    ) -> tuple[FieldSpec, ...] | None:
        wanted = target_type.strip().lower()
        for type_name, specs in catalog.items():
            if type_name.strip().lower() == wanted:
                return specs
        return None

    def _resolve_property(
        self,
        specs: tuple[FieldSpec, ...] | None,
        target_type: str,
        identifier: str,
    ) -> tuple[str, str]:
        """-> (fields key to read/write, format or "" when unknown)."""
        if specs is None:
            return identifier, ""
        wanted = identifier.strip().lower()
        for spec in specs:
            if wanted in (spec.key.strip().lower(), spec.name.strip().lower()):
                return spec.key or spec.name, spec.format
        hints = ", ".join(spec.render_hint() for spec in specs)
        raise SchemaViolation(
            f"type {target_type!r} has no property {identifier!r}; "
            f"its properties: {hints or '(none)'}. Every type also has "
            f"the built-in {rules.BUILTIN_WATCH_MODIFIED!r} (the store's "
            "last-modified stamp) -- watchable with condition 'changed', "
            "never writable"
        )

    def _note_overlaps(self, bound: list[_BoundRule]) -> None:
        """A rule watching a property another rule writes never sees the
        engine's writes (the baseline absorbs them) — worth one log line
        per pair so a human expecting a cascade learns why it stays
        quiet. Informational, never an error."""
        for watcher in bound:
            if not watcher.watches:
                continue  # a manual rule watches nothing (ADR 058)
            for writer in bound:
                if writer.node.id == watcher.node.id:
                    continue
                if not writer.action_key.strip():
                    # A script rule has no fixed write target; without
                    # this, two script rules' empty keys would compare
                    # equal and log a cascade that cannot exist.
                    continue
                pair = (writer.node.id, watcher.node.id)
                if pair in self._noted_overlaps:
                    continue
                if (
                    watcher.read_key.strip().lower()
                    == writer.action_key.strip().lower()
                ):
                    self._noted_overlaps.add(pair)
                    logger.info(
                        "rule %r watches %r, which rule %r writes; engine "
                        "writes never trigger rules, so they will not "
                        "cascade", watcher.node.name, watcher.config.
                        watch_property, writer.node.name,
                    )

    # -- transition detection ----------------------------------------------

    def _plan(
        self,
        bound: list[_BoundRule],
        prior: dict[NodeId, dict[str, str]] | None,
    ) -> list[tuple[_BoundRule, Node, str, str]]:
        """The (rule, object, before, after) fires, from tick-start state.

        Values are compared against the PRIOR baseline; an object or key
        the baseline has never seen is recorded silently, never fired
        (startup, newly created objects, newly enabled rules). For an
        uncheck-others rule with several same-tick flips, only the last
        (node-id order) executes — deterministic last-writer-wins; the
        earlier transitions are consumed without action.
        """
        if prior is None:
            return []
        plans: list[tuple[_BoundRule, Node, str, str]] = []
        for rule in bound:
            if not rule.watches:
                continue  # a manual rule: nothing to diff (ADR 058)
            matches: list[tuple[_BoundRule, Node, str, str]] = []
            for obj in sorted(self._targets(rule), key=lambda n: n.id):
                before = prior.get(obj.id, {}).get(rule.read_key)
                if before is None:
                    continue  # unseen: baseline silently at tick end
                after = self._watched_value(rule, obj)
                if rules.condition_met(rule.config.condition, before, after):
                    matches.append((rule, obj, before, after))
            if rule.config.action == rules.ACTION_UNCHECK_OTHERS and matches:
                matches = [matches[-1]]
            plans.extend(matches)
        return plans

    def _targets(self, rule: _BoundRule) -> list[Node]:
        wanted = rule.config.target_type.strip().lower()
        return [
            node for node in self._repository.graph.nodes()
            if node.role not in INFRA_ROLES
            and wanted in (
                node.type.strip().lower(), node.type_key.strip().lower()
            )
        ]

    def _collect(
        self, bound: list[_BoundRule]
    ) -> dict[NodeId, dict[str, str]]:
        """Current watched values off the live index (the new baseline)."""
        snapshot: dict[NodeId, dict[str, str]] = {}
        for rule in bound:
            if not rule.watches:
                continue  # a manual rule baselines nothing (ADR 058)
            for obj in self._targets(rule):
                snapshot.setdefault(obj.id, {})[rule.read_key] = (
                    self._watched_value(rule, obj)
                )
        return snapshot

    def _watched_value(self, rule: _BoundRule, obj: Node) -> str:
        """The rule's watched value on one object: the store-clock stamp
        for a built-in watch (ADR 042), else the fields value."""
        if rule.builtin_watch:
            return obj.modified_at
        return self._field_of(obj.fields, rule.read_key)

    @staticmethod
    def _field_of(fields: Mapping[str, str], key: str) -> str:
        """A fields value by exact key, else case-insensitive match, else
        "" — absence IS the false/empty value (the adapter drops unticked
        checkboxes and empty values from ``Node.fields``)."""
        value = fields.get(key)
        if value is not None:
            return value
        wanted = key.strip().lower()
        for stored, stored_value in fields.items():
            if stored.strip().lower() == wanted:
                return stored_value
        return ""

    # -- actions -----------------------------------------------------------

    def _now_value(self, action_format: str) -> str:
        """What ``set property to now`` writes, by target format (R2).

        The live server rejects naive timestamps on date properties;
        text targets carry the full local stamp, date targets get local
        midnight with its explicit UTC offset (see ``_local_midnight``).
        """
        now = self._now()
        if action_format == "date":
            return _local_midnight(now, self._zone)
        return _stamp(now)

    async def _execute(
        self, rule: _BoundRule, obj: Node, before: str, after: str
    ) -> None:
        if rule.config.action == rules.ACTION_RUN_SCRIPT:
            await self._execute_script(rule, obj, before, after)
        elif rule.config.action == rules.ACTION_SET_NOW:
            await self._write_field(
                obj, rule.action_key, self._now_value(rule.action_format)
            )
        elif rule.config.action == rules.ACTION_SET_VALUE:
            await self._write_field(obj, rule.action_key, rule.config.action_value)
        else:  # ACTION_UNCHECK_OTHERS
            # Execution-time sibling state (post earlier same-tick
            # writes): already-unchecked siblings get no write.
            for sibling in sorted(self._targets(rule), key=lambda n: n.id):
                if sibling.id == obj.id:
                    continue
                current = self._field_of(sibling.fields, rule.action_key)
                if current.strip().lower() == "true":
                    await self._write_field(sibling, rule.action_key, "false")

    async def _execute_script(
        self, rule: _BoundRule, obj: Node, before: str, after: str
    ) -> None:
        """One sandboxed script fire (WP32): snapshot -> run -> apply.

        Effects are ALL validated before ANY write; a mid-apply write
        failure keeps the writes already applied (transition-consumed,
        no-retry -- the engine's standing semantics)."""
        assert self._script_runner is not None  # _attach_scripts gated
        payload = self._script_payload(rule, obj, before, after)
        outcome = await self._script_runner.run(rule.script, payload)
        for line in outcome.logs:
            logger.info("rule %r script: %s", rule.node.name, line)
        for target, key, value in self._validated_effects(outcome):
            await self._write_field(target, key, value)

    def _script_payload(
        self, rule: _BoundRule, obj: Node, before: str, after: str
    ) -> dict[str, Any]:
        """The child's world: every non-infra node + the edges among
        them, plus the trigger context. Loud past the size cap."""
        graph = self._repository.graph
        exported = [n for n in graph.nodes() if n.role not in INFRA_ROLES]
        if len(exported) > _SCRIPT_MAX_NODES:
            raise GraphContextError(
                f"the space is too large for scripts: {len(exported)} "
                f"objects exceeds the {_SCRIPT_MAX_NODES}-object snapshot cap"
            )
        ids = {n.id for n in exported}
        edges = [
            {"source": edge.source, "type": edge.type, "target": edge.target}
            for n in exported
            for edge in graph.edges(n.id, Direction.OUT)
            if edge.target in ids
        ]
        return {
            "now": _stamp(self._now()),
            "rule": {"id": rule.node.id, "name": rule.node.name},
            "trigger": obj.id,
            "before": before,
            "after": after,
            "nodes": [
                {
                    "id": n.id, "type": n.type, "name": n.name,
                    "summary": n.summary, "fields": dict(n.fields),
                }
                for n in exported
            ],
            "edges": edges,
            "caps": {"max_sets": _SCRIPT_MAX_SETS},
        }

    def _validated_effects(
        self, outcome: ScriptOutcome
    ) -> list[tuple[Node, str, str]]:
        """Resolve every queued write, or raise naming the offender --
        nothing is applied unless everything validates."""
        if len(outcome.sets) > _SCRIPT_MAX_SETS:
            raise GraphContextError(
                f"the script queued {len(outcome.sets)} writes; the cap "
                f"is {_SCRIPT_MAX_SETS} per fire"
            )
        graph = self._repository.graph
        catalog = self._repository.field_catalog()
        planned: list[tuple[Node, str, str]] = []
        for effect in outcome.sets:
            if not graph.has_node(effect.node_id):
                raise GraphContextError(
                    f"the script set() targets unknown object "
                    f"{effect.node_id!r} -- use ids from objects()/find()"
                )
            target = graph.node(effect.node_id)
            if target.role in INFRA_ROLES:
                raise GraphContextError(
                    f"the script may not modify {target.name!r} "
                    f"({target.type}): system objects are off limits"
                )
            specs = self._type_specs(catalog, target.type)
            key, fmt = self._resolve_property(specs, target.type, effect.property)
            if fmt == "checkbox":
                domain_fields.parse_checkbox(key, effect.value)
            elif fmt == "number":
                domain_fields.parse_number(key, effect.value)
            planned.append((target, key, effect.value))
        return planned

    async def _write_field(self, node: Node, key: str, value: str) -> None:
        # The full merged map, not a delta: the in-memory backend
        # replaces ``fields`` wholesale on update (the Scheduler rule).
        # Re-read the node — an earlier write this tick may have
        # refreshed it — then merge.
        fresh = self._repository.graph.node(node.id)
        merged = {**dict(fresh.fields), key: value}
        await self._repository.update_node(fresh.id, fields=merged)

    # -- bookkeeping -------------------------------------------------------

    async def _write_bookkeeping(
        self,
        bound: list[_BoundRule],
        broken: list[tuple[Node, str]],
        fired: list[RuleFiring],
        failures: dict[NodeId, str],
    ) -> RuleTickReport:
        """Settle each rule node's status/last-error/last-fired, writing
        only when the stored values differ (no per-tick write spam)."""
        errors: list[RuleProblem] = []
        healed: list[NodeId] = []
        fired_rule_ids = {firing.rule_id for firing in fired}
        for node, message in broken:
            changes: dict[str, str] = {}
            if node.fields.get(rules.FIELD_STATUS, "") != rules.STATUS_ERROR:
                changes[rules.FIELD_STATUS] = rules.STATUS_ERROR
            if node.fields.get(rules.FIELD_LAST_ERROR, "") != message:
                changes[rules.FIELD_LAST_ERROR] = message
            if changes:
                await self._write_rule_fields(node, changes)
                errors.append(RuleProblem(
                    rule_id=node.id, rule_name=node.name, message=message,
                ))
        for rule in bound:
            node = rule.node
            changes = {}
            stored_status = node.fields.get(rules.FIELD_STATUS, "").strip()
            if stored_status.lower() == rules.STATUS_ERROR.lower():
                # Self-heal: the config parses again — the human fixed it.
                changes[rules.FIELD_STATUS] = rules.STATUS_ACTIVE
                changes[rules.FIELD_LAST_ERROR] = ""
                healed.append(node.id)
            failure = failures.get(node.id)
            if failure is not None:
                # An action write failed; the transition is consumed (no
                # retry). Status stays — the message persists until the
                # next successful fire clears it.
                if node.fields.get(rules.FIELD_LAST_ERROR, "") != failure:
                    changes[rules.FIELD_LAST_ERROR] = failure
                errors.append(RuleProblem(
                    rule_id=node.id, rule_name=node.name, message=failure,
                ))
            elif node.id in fired_rule_ids:
                changes[rules.FIELD_LAST_FIRED] = _stamp(self._now())
                if node.fields.get(rules.FIELD_LAST_ERROR, ""):
                    changes[rules.FIELD_LAST_ERROR] = ""
            if changes:
                await self._write_rule_fields(node, changes)
        return RuleTickReport(
            fired=tuple(fired), errors=tuple(errors), healed=tuple(healed),
        )

    async def _write_rule_fields(
        self, node: Node, changes: dict[str, str]
    ) -> None:
        try:
            # Re-read before merging, like _write_field: ``node`` is the
            # snapshot _load_rules took at the top of the tick, and the
            # tick may have written the rule since -- the run-now claim
            # (ADR 058) does exactly that, so merging the stale map here
            # would re-tick the box and the rule would fire forever.
            fresh = self._repository.graph.node(node.id)
            merged = {**dict(fresh.fields), **changes}
            await self._repository.update_node(fresh.id, fields=merged)
        except GraphContextError as err:
            # Bookkeeping must never take the tick down; the state is
            # re-derived next tick anyway.
            logger.warning(
                "could not update rule %s bookkeeping: %s", node.id, err
            )
