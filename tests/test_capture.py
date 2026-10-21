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


def test_sqlite_budget_preflight_refuses_export_before_writing(tmp_path):
    from bbackup.resource_limits import ResourceLimitError
    original = tmp_path / "live.sqlite"
    with sqlite3.connect(original) as database:
        database.execute("CREATE TABLE data(value TEXT)")
        database.execute("INSERT INTO data VALUES ('committed')")

    class Monitor:
        def check(self, upcoming=0):
            if upcoming > 10:
                raise ResourceLimitError("Observed free-space reserve fell below the configured threshold")

    with pytest.raises(ResourceLimitError, match="reserve"):
        with capture_sources((Source("db", "sqlite", (str(original),)),), tmp_path,
                             resource_monitor=Monitor()):
            pass
    assert not list(tmp_path.glob("capture-*"))


def test_native_export_checks_budget_before_writing():
    import io
    from bbackup.capture import _bounded_write
    from bbackup.resource_limits import ResourceLimitError

    class Monitor:
        def check(self, upcoming=0):
            if upcoming > 4:
                raise ResourceLimitError("Observed free-space reserve fell below the configured threshold")

    output = io.BytesIO()
    with pytest.raises(ResourceLimitError, match="reserve"):
        _bounded_write(output, b"database export", Monitor())
    assert output.getvalue() == b""


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
