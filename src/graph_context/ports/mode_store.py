"""ModeStore port: the space's Activity Mode config objects (ADR 015).

In-space mode configuration makes the deployment's activity modes human
data: an ``Activity Mode`` object in Anytype defines one mode (name, goal
prompt, tool binding, capture policy), edited in the app like everything
else. The port deals in plain payload dicts; validation and precedence
are the loader's job (``orchestrator/modes.py``), and every Anytype
representation detail stays quarantined in the adapter.

Payload shape (one dict per non-archived mode object)::

    {
        "name": str,       # the object's display name, unslugged
        "goal": str,       # the page body, verbatim
        "mutating": bool,
        "capture": {       # or None when no capture is configured
            "artifact_type": str,
            "references_label": str,   # present only if set
            "min_chars": int,          # present only if set
        },
        "origin": str,     # "<name> (<object id>)" for error messages
    }

Contract:
* ``load`` returns every candidate, malformed ones included -- the loader
  owns rejection so its errors are uniform across config sources.
* Store I/O failures raise; callers decide whether that is fatal
  (startup) or degradable (a ``/mode`` refresh).
"""

from __future__ import annotations

from typing import Any, Protocol


class ModeStore(Protocol):
    async def load(self) -> list[dict[str, Any]]: ...
