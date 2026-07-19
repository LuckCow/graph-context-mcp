"""Port: sandboxed execution of an Automation Rule's script (WP32, ADR 040).

The rule engine hands a script and a self-contained JSON-able payload
(graph snapshot + trigger context) to a runner and gets back the writes
the script queued. The port keeps the engine ignorant of HOW isolation
happens -- the production implementation is a rlimited subprocess
(``infrastructure/sandbox``); tests substitute a fake that returns
canned outcomes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ScriptEffect:
    """One property write a script queued via its ``set()`` helper.

    ``node_id`` is whatever the script supplied -- the ENGINE validates
    it against the graph (existence, non-infra role) and resolves
    ``property`` against the target's type before anything is applied.
    ``value`` is already coerced to the wire string ("true"/"false" for
    booleans, plain digits for numbers).
    """

    node_id: str
    property: str
    value: str


@dataclass(frozen=True, slots=True)
class ScriptOutcome:
    """What a script run produced: queued writes plus its log lines."""

    sets: tuple[ScriptEffect, ...] = ()
    logs: tuple[str, ...] = ()


class ScriptRunner(Protocol):
    """Executes one script in isolation.

    Contract: raises :class:`graph_context.errors.GraphContextError`
    with a human-legible message on ANY failure -- timeout, nonzero
    exit (the message carries the script's traceback tail), oversized
    or malformed output -- because the message's destination is the
    rule object's ``gc_rule_last_error`` field, read by the person (or
    LLM) fixing the script.
    """

    async def run(
        self, script: str, payload: Mapping[str, Any]
    ) -> ScriptOutcome: ...
