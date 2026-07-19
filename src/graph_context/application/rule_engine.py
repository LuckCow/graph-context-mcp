"""Use-case: detecting property transitions and firing Automation Rules
(WP31, ADR 039).

An Automation Rule is a ``gc_rule`` node (``domain/rules.py`` owns the
vocabulary) watching one scalar property on one object type. The engine
runs one :meth:`RuleEngine.run_tick` per poll (the Anytype bot's
``_watch_rules`` loop, under the space's turn lock, right after a
resync), diffing a private in-memory baseline of watched values against
the current index:

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
from dataclasses import dataclass
from datetime import datetime

from graph_context.domain import fields as domain_fields
from graph_context.domain import rules
from graph_context.domain.models import FieldSpec, Node, NodeId
from graph_context.domain.schema import INFRA_ROLES, Role
from graph_context.errors import GraphContextError, SchemaViolation
from graph_context.ports.graph_repository import GraphRepository

logger = logging.getLogger(__name__)

_ERROR_LIMIT = 300  # gc_rule_last_error cap: a field, not a traceback


def _local_now() -> datetime:
    # Naive local wall-clock time, the scheduler's convention; the
    # composition root injects local_clock(GC_TIMEZONE) instead.
    return datetime.now()


def _stamp(moment: datetime) -> str:
    return moment.isoformat(sep=" ", timespec="seconds")


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


@dataclass(frozen=True, slots=True)
class _BoundRule:
    """A parsed rule resolved against the space: which fields key to
    read, which to write, and the write target's format ("" unknown)."""

    node: Node
    config: rules.RuleConfig
    read_key: str
    action_key: str
    action_format: str


class RuleEngine:
    """Automation-rule reads and writes over the shared repository."""

    def __init__(
        self,
        repository: GraphRepository,
        now: Callable[[], datetime] = _local_now,
    ) -> None:
        self._repository = repository
        self._now = now
        # object id -> {watched fields key -> last-seen value}. None =
        # never baselined (first tick records, never fires).
        self._snapshot: dict[NodeId, dict[str, str]] | None = None
        self._noted_overlaps: set[tuple[NodeId, NodeId]] = set()

    async def run_tick(self) -> RuleTickReport:
        """One diff-fire-rebaseline pass over the shared index."""
        bound, broken = self._load_rules()
        self._note_overlaps(bound)
        prior = self._snapshot
        plans = self._plan(bound, prior)
        fired: list[RuleFiring] = []
        failures: dict[NodeId, str] = {}
        for rule, obj in plans:
            try:
                await self._execute(rule, obj)
            except GraphContextError as err:
                failures[rule.node.id] = _truncate(str(err))
                continue
            fired.append(RuleFiring(
                rule_id=rule.node.id,
                rule_name=rule.node.name,
                node_id=obj.id,
                node_name=obj.name,
                action=rule.config.action,
            ))
        report = await self._write_bookkeeping(bound, broken, fired, failures)
        # Rebaseline from the POST-action index: the engine's own writes
        # are folded in before the next diff, so they never fire rules.
        self._snapshot = self._collect(bound)
        return report

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
        read_key, read_format = self._resolve_property(
            specs, config.target_type, config.watch_property
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
        )

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
            f"its properties: {hints or '(none)'}"
        )

    def _note_overlaps(self, bound: list[_BoundRule]) -> None:
        """A rule watching a property another rule writes never sees the
        engine's writes (the baseline absorbs them) — worth one log line
        per pair so a human expecting a cascade learns why it stays
        quiet. Informational, never an error."""
        for watcher in bound:
            for writer in bound:
                if writer.node.id == watcher.node.id:
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
    ) -> list[tuple[_BoundRule, Node]]:
        """The (rule, object) pairs to fire, from tick-start state.

        Values are compared against the PRIOR baseline; an object or key
        the baseline has never seen is recorded silently, never fired
        (startup, newly created objects, newly enabled rules). For an
        uncheck-others rule with several same-tick flips, only the last
        (node-id order) executes — deterministic last-writer-wins; the
        earlier transitions are consumed without action.
        """
        if prior is None:
            return []
        plans: list[tuple[_BoundRule, Node]] = []
        for rule in bound:
            matches: list[tuple[_BoundRule, Node]] = []
            for obj in sorted(self._targets(rule), key=lambda n: n.id):
                before = prior.get(obj.id, {}).get(rule.read_key)
                if before is None:
                    continue  # unseen: baseline silently at tick end
                after = self._field_of(obj.fields, rule.read_key)
                if rules.condition_met(rule.config.condition, before, after):
                    matches.append((rule, obj))
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
            for obj in self._targets(rule):
                snapshot.setdefault(obj.id, {})[rule.read_key] = (
                    self._field_of(obj.fields, rule.read_key)
                )
        return snapshot

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

    async def _execute(self, rule: _BoundRule, obj: Node) -> None:
        if rule.config.action == rules.ACTION_SET_NOW:
            # A date-format target gets the bare LOCAL date: the live
            # server rejects naive timestamps and accepts RFC 3339 only
            # WITH a timezone -- which our naive-local clock convention
            # cannot honestly supply (WP31 e2e probe, R2). Text targets
            # carry the full stamp.
            now = self._now()
            value = (
                now.date().isoformat() if rule.action_format == "date"
                else _stamp(now)
            )
            await self._write_field(obj, rule.action_key, value)
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
            merged = {**dict(node.fields), **changes}
            await self._repository.update_node(node.id, fields=merged)
        except GraphContextError as err:
            # Bookkeeping must never take the tick down; the state is
            # re-derived next tick anyway.
            logger.warning(
                "could not update rule %s bookkeeping: %s", node.id, err
            )
