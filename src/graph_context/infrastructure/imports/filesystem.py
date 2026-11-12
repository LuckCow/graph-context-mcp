"""A directory of JSON exports as an :class:`ImportSource` (ADR 062).

Each ``*.json`` file is one document whose kind is the file stem
(``character.json`` -> ``character``). The plugin rewrites files
periodically (and may one day write straight into a bind mount), so the
adapter is built for that:

* **Cheap when nothing moved.** A file re-decodes only when its
  ``(mtime_ns, size)`` changed; an unchanged directory costs one
  ``stat`` per file per tick.
* **Torn reads keep the last good decode.** A file caught mid-write
  fails ``json.loads``; the previous decode stands for this tick (or the
  file is omitted if it never decoded) and the next tick retries. Logged
  at warning ONCE per bad version, not every tick.
* **A vanished directory is loud.** ``read`` raises
  :class:`GraphContextError`; the change tick's listener guard logs it
  each tick, which is the right volume for an ops mistake.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from graph_context.domain.imports import ImportDocument
from graph_context.errors import GraphContextError

logger = logging.getLogger(__name__)


class DirectoryImportSource:
    def __init__(self, directory: Path) -> None:
        if not directory.is_dir():
            raise GraphContextError(
                f"import_dir {directory} is not a directory the bot can read"
            )
        self._dir = directory
        # stem -> (version, document). version = (mtime_ns, size).
        self._decoded: dict[str, tuple[tuple[int, int], ImportDocument]] = {}
        self._warned: dict[str, tuple[int, int]] = {}

    async def read(self) -> tuple[ImportDocument, ...]:
        try:
            paths = sorted(p for p in self._dir.iterdir() if p.suffix == ".json")
        except OSError as err:
            raise GraphContextError(
                f"cannot list import_dir {self._dir}: {err}"
            ) from None
        documents: list[ImportDocument] = []
        for path in paths:
            document = self._document(path)
            if document is not None:
                documents.append(document)
        return tuple(documents)

    def _document(self, path: Path) -> ImportDocument | None:
        kind = path.stem
        try:
            stat = path.stat()
        except OSError:  # deleted between listing and stat
            self._decoded.pop(kind, None)
            return None
        version = (stat.st_mtime_ns, stat.st_size)
        held = self._decoded.get(kind)
        if held is not None and held[0] == version:
            return held[1]
        payload = self._decode(path, kind, version)
        if payload is None:
            return held[1] if held is not None else None
        document = ImportDocument(kind=kind, payload=payload)
        self._decoded[kind] = (version, document)
        self._warned.pop(kind, None)
        return document

    def _decode(
        self, path: Path, kind: str, version: tuple[int, int]
    ) -> Mapping[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            if self._warned.get(kind) != version:
                self._warned[kind] = version
                logger.warning(
                    "import %s: %s is not readable JSON yet (%s); keeping the "
                    "previous export until it is", kind, path.name, err,
                )
            return None
        if not isinstance(payload, dict):
            if self._warned.get(kind) != version:
                self._warned[kind] = version
                logger.warning(
                    "import %s: %s is not a JSON object; ignored", kind, path.name
                )
            return None
        return payload
