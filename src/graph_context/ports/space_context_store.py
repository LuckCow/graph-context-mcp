"""SpaceContextStore port: the space's settings singleton (ADR 034).

A ``Space Context`` object in Anytype carries space-wide assistant
configuration -- today, an ``objects``-format link naming the Activity
Mode NEW chats start in. Like :mod:`graph_context.ports.mode_store`, the
port deals in plain payload dicts; validation (the singleton rule, link
arity, resolving the link to a loaded mode) is the loader's job
(``orchestrator/modes.py``), and every Anytype representation detail
stays quarantined in the adapter.

Payload shape (one dict per non-archived Space Context object)::

    {
        "name": str,                    # the object's display name
        "default_mode_ids": list[str],  # gc_default_mode link targets
        "origin": str,                  # "<name> (<object id>)" for errors
    }

Contract:
* ``load`` returns every candidate, extras included -- the loader owns
  the singleton rule so its errors are uniform across config sources.
* Store I/O failures raise; callers decide whether that is fatal
  (startup) or degradable (a ``/mode`` refresh).
"""

from __future__ import annotations

from typing import Any, Protocol


class SpaceContextStore(Protocol):
    async def load(self) -> list[dict[str, Any]]: ...
