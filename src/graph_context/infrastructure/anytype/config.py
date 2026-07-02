"""Configuration and errors for the Anytype adapter.

Everything tunable about how we talk to Anytype lives in
:class:`AnytypeConfig`; nothing in the adapter reads the environment
directly. ``from_env`` is the only place env-var names appear.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from graph_context.errors import GraphContextError

API_VERSION = "2025-11-08"  # pinned; bump deliberately (see changelog risk)
DEFAULT_BASE_URL = "http://localhost:31009"


class AnytypeApiError(GraphContextError):
    """An HTTP-level failure talking to the Anytype API."""

    def __init__(self, status: int, code: str, message: str, endpoint: str) -> None:
        super().__init__(f"Anytype API error {status} ({code}) at {endpoint}: {message}")
        self.status = status
        self.code = code
        self.detail = message  # raw server message, for targeted handling
        self.endpoint = endpoint


class SyncError(GraphContextError):
    """Hydrate/resync could not complete coherently."""


@dataclass(frozen=True, slots=True)
class AnytypeConfig:
    """Connection + behavior settings for the Anytype adapter."""

    api_key: str
    space_id: str
    base_url: str = DEFAULT_BASE_URL
    api_version: str = API_VERSION
    # Spike S2: GET /objects honors large pages (no observed cap <=1000), so the
    # full hydrate sweep uses a big page and finishes in 2-3 calls for ~2k nodes.
    page_limit: int = 1000
    # Spike S2: POST /search (the only filtered endpoint -- used by resync) is
    # hard-capped at 100/page server-side; requesting more is silently clamped.
    search_page_limit: int = 100
    max_retries: int = 3
    backoff_base_seconds: float = 0.25
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> AnytypeConfig:
        api_key = _resolve_api_key()
        try:
            space_id = os.environ["ANYTYPE_SPACE_ID"]
        except KeyError as missing:
            raise GraphContextError(
                f"missing required environment variable: {missing.args[0]}"
            ) from None
        # In the container the secret is file-mounted and the base URL points at
        # the host's Anytype (host.docker.internal); both arrive under the
        # ``*_API_*`` names. Accept the bare names too for a host-local run.
        base_url = (
            os.environ.get("ANYTYPE_BASE_URL")
            or os.environ.get("ANYTYPE_API_BASE_URL")
            or DEFAULT_BASE_URL
        )
        return cls(api_key=api_key, space_id=space_id, base_url=base_url)


def _resolve_api_key() -> str:
    """The key, from ``ANYTYPE_API_KEY`` or a file at ``ANYTYPE_API_KEY_FILE``.

    The container mounts the key as a read-only file (env vars leak via
    ``docker inspect`` / ``/proc``), so the file path is the primary source;
    the inline env var is the host-local fallback.
    """
    inline = os.environ.get("ANYTYPE_API_KEY")
    if inline:
        return inline
    path = os.environ.get("ANYTYPE_API_KEY_FILE")
    if path:
        try:
            with open(path) as handle:
                key = handle.read().strip()
        except OSError as err:
            raise GraphContextError(
                f"could not read ANYTYPE_API_KEY_FILE at {path}: {err}"
            ) from None
        if key:
            return key
    raise GraphContextError(
        "missing Anytype credentials: set ANYTYPE_API_KEY or ANYTYPE_API_KEY_FILE"
    )
