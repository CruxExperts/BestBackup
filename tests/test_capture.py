import sqlite3
import json
from pathlib import Path
from threading import Event

import pytest

from bbackup.capture import CaptureError, capture_sources
from bbackup.models import Source


def test_sqlite_transactional_export_and_cleanup(tmp_path):
    original = tmp_path / "live.sqlite"
    writer = sqlite3.connect(original)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE data (value TEXT)")
    writer.execute("INSERT INTO data VALUES ('committed')")
    writer.commit()
    writer.execute("INSERT INTO data VALUES ('uncommitted')")
    with capture_sources(
        (Source("db", "sqlite", (str(original),)),), tmp_path
    ) as paths:
        exported = Path(paths[0])
        with sqlite3.connect(exported) as restored:
            assert restored.execute("SELECT value FROM data").fetchall() == [
                ("committed",)
            ]
        assert exported.stat().st_mode & 0o777 == 0o600
        manifest = json.loads(Path(paths[-1]).read_text())
        assert manifest == {"schema_version": 2, "exports": [
            {"source": "db", "kind": "sqlite", "format": "sqlite", "path": str(exported)}
        ]}
    assert not exported.exists()
    writer.rollback()
    writer.close()


def test_files_not_copied(tmp_path):
    source = Source("files", "files", (str(tmp_path),))
    with capture_sources((source,), tmp_path) as paths:
        assert paths == [str(tmp_path)]


def test_cancellation_and_unsupported_fail_closed(tmp_path):
    cancel = Event()
    cancel.set()
    with (
        pytest.raises(CaptureError),
        capture_sources(
            (Source("a", "files", (str(tmp_path),)),), tmp_path, cancel=cancel
        ),
    ):
        pass
    with (
        pytest.raises(CaptureError),
        capture_sources((Source("db", "mysql", ("unused",)),), tmp_path),
    ):
        pass
