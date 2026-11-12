"""The directory import source (ADR 062): cheap when unchanged, torn
reads keep the last good decode, a vanished directory is loud."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from graph_context.errors import GraphContextError
from graph_context.infrastructure.imports.filesystem import DirectoryImportSource


def _write(path: Path, payload: object, *, mtime_ns: int | None = None) -> None:
    path.write_text(json.dumps(payload) if not isinstance(payload, str) else payload)
    if mtime_ns is not None:  # make "changed" explicit: coarse clocks lie
        os.utime(path, ns=(mtime_ns, mtime_ns))


class TestRead:
    async def test_each_json_file_is_one_document_keyed_by_stem(self, tmp_path: Path) -> None:
        _write(tmp_path / "character.json", {"account_name": "LuckCow"})
        _write(tmp_path / "quests.json", {"quests": []})
        (tmp_path / "notes.txt").write_text("not an export")
        docs = await DirectoryImportSource(tmp_path).read()
        assert [(d.kind, dict(d.payload)) for d in docs] == [
            ("character", {"account_name": "LuckCow"}),
            ("quests", {"quests": []}),
        ]

    async def test_an_unchanged_file_is_not_decoded_again(self, tmp_path: Path) -> None:
        _write(tmp_path / "character.json", {"v": 1}, mtime_ns=1_000)
        source = DirectoryImportSource(tmp_path)
        (first,) = await source.read()
        (again,) = await source.read()
        assert again is first  # the cached decode, not a fresh object

    async def test_a_changed_file_is_re_read(self, tmp_path: Path) -> None:
        _write(tmp_path / "character.json", {"v": 1}, mtime_ns=1_000)
        source = DirectoryImportSource(tmp_path)
        await source.read()
        _write(tmp_path / "character.json", {"v": 2}, mtime_ns=2_000)
        (doc,) = await source.read()
        assert doc.payload == {"v": 2}

    async def test_a_deleted_file_drops_out(self, tmp_path: Path) -> None:
        _write(tmp_path / "character.json", {"v": 1})
        source = DirectoryImportSource(tmp_path)
        await source.read()
        (tmp_path / "character.json").unlink()
        assert await source.read() == ()


class TestTornReads:
    async def test_a_mid_write_file_keeps_the_previous_decode(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write(tmp_path / "character.json", {"v": 1}, mtime_ns=1_000)
        source = DirectoryImportSource(tmp_path)
        await source.read()
        _write(tmp_path / "character.json", '{"v": 2, "stats": {', mtime_ns=2_000)
        with caplog.at_level(logging.WARNING):
            (doc,) = await source.read()
            await source.read()  # same bad version: no second warning
        assert doc.payload == {"v": 1}
        assert sum("not readable JSON yet" in r.message for r in caplog.records) == 1

    async def test_a_file_that_never_decoded_is_omitted(self, tmp_path: Path) -> None:
        _write(tmp_path / "character.json", "{oops")
        _write(tmp_path / "quests.json", {"quests": []})
        docs = await DirectoryImportSource(tmp_path).read()
        assert [d.kind for d in docs] == ["quests"]

    async def test_the_next_good_write_replaces_the_stale_decode(self, tmp_path: Path) -> None:
        _write(tmp_path / "character.json", {"v": 1}, mtime_ns=1_000)
        source = DirectoryImportSource(tmp_path)
        await source.read()
        _write(tmp_path / "character.json", "{torn", mtime_ns=2_000)
        await source.read()
        _write(tmp_path / "character.json", {"v": 3}, mtime_ns=3_000)
        (doc,) = await source.read()
        assert doc.payload == {"v": 3}

    async def test_a_non_object_document_is_ignored(self, tmp_path: Path) -> None:
        _write(tmp_path / "list.json", [1, 2, 3])
        assert await DirectoryImportSource(tmp_path).read() == ()


class TestDirectory:
    def test_a_missing_directory_fails_at_construction(self, tmp_path: Path) -> None:
        with pytest.raises(GraphContextError, match="import_dir"):
            DirectoryImportSource(tmp_path / "nope")

    async def test_a_vanished_directory_is_loud_on_read(self, tmp_path: Path) -> None:
        target = tmp_path / "exports"
        target.mkdir()
        source = DirectoryImportSource(target)
        target.rmdir()
        with pytest.raises(GraphContextError, match="cannot list"):
            await source.read()
