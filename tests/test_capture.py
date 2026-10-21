import sqlite3
import json
import hashlib
from pathlib import Path
from threading import Event

import pytest

from bbackup.capture import CaptureError, DurableCaptureArtifacts, capture_sources
from bbackup.linux_coverage import sqlite_file_identity
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


def test_covered_sqlite_manifest_binds_export_to_reviewed_source(tmp_path):
    original = tmp_path / "database [one]*?.sqlite"
    with sqlite3.connect(original) as database:
        database.execute("CREATE TABLE committed (value TEXT)")
        database.execute("INSERT INTO committed VALUES ('covered')")
    context = {"root_id": "database", "path": str(original), "source_id": "db",
               "identity": sqlite_file_identity(original), "recipe_digest": "a" * 64,
               "baseline_digest": "b" * 64}
    with capture_sources((Source("db", "sqlite", (str(original),)),), tmp_path,
                         coverage_sqlite={"db": context}) as paths:
        exported = Path(paths[0])
        manifest = json.loads(Path(paths[-1]).read_text())
        assert manifest["schema_version"] == 2
        assert len(manifest["exports"]) == 1
        entry = manifest["exports"][0]
        assert entry["coverage_root_id"] == "database"
        assert entry["original_path"] == str(original)
        assert entry["reviewed_identity"] == context["identity"]
        assert entry["recipe_digest"] == "a" * 64
        assert entry["baseline_digest"] == "b" * 64
        assert entry["export_sha256"] == hashlib.sha256(exported.read_bytes()).hexdigest()
        with sqlite3.connect(exported) as database:
            assert database.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert database.execute("SELECT value FROM committed").fetchall() == [("covered",)]
    with pytest.raises(CaptureError, match="identity changed"):
        original.chmod(0o600)
        with capture_sources((Source("db", "sqlite", (str(original),)),), tmp_path,
                             coverage_sqlite={"db": context}):
            pass


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


def test_durable_first_export_survives_later_source_failure(tmp_path):
    staging = tmp_path / "staging"
    state = tmp_path / "state"
    staging.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    original = tmp_path / "live.sqlite"
    with sqlite3.connect(original) as database:
        database.execute("CREATE TABLE data(value TEXT)")
        database.execute("INSERT INTO data VALUES ('first point in time')")
    artifacts = DurableCaptureArtifacts(staging, state, "a" * 64, "b" * 32, "daily")
    with pytest.raises(CaptureError, match="existing database"):
        with capture_sources((Source("first", "sqlite", (str(original),)),
                              Source("second", "sqlite", (str(tmp_path / "missing"),))),
                             staging, durable_artifacts=artifacts):
            pytest.fail("later failed export became a backup")
    record = json.loads(artifacts.record_path.read_text())
    assert record["phase"] == "prepared"
    assert [item["name"] for item in record["exports"]] == ["first.sqlite"]
    exported = artifacts.directory / "first.sqlite"
    assert record["exports"][0]["sha256"] == hashlib.sha256(exported.read_bytes()).hexdigest()
    with sqlite3.connect(original) as database:
        database.execute("UPDATE data SET value='later state'")
    with sqlite3.connect(exported) as database:
        assert database.execute("SELECT value FROM data").fetchone() == ("first point in time",)


def test_durable_cleanup_rejects_tampered_record_path(tmp_path):
    staging = tmp_path / "staging"
    state = tmp_path / "state"
    staging.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    artifacts = DurableCaptureArtifacts(staging, state, "a" * 64, "b" * 32, "daily")
    artifacts.prepare()
    exported = artifacts.directory / "db.dump"
    artifacts.begin_export(exported)
    exported.write_bytes(b"point in time")
    exported.chmod(0o600)
    artifacts.finish_export(exported)
    manifest = artifacts.directory / "bbackup-capture.json"
    artifacts.begin_export(manifest)
    manifest.write_text("{}")
    manifest.chmod(0o600)
    artifacts.ready(manifest)
    outside = staging / "foreign"
    outside.write_bytes(b"keep")
    record = json.loads(artifacts.record_path.read_text())
    record["phase"] = "cleanup_started"
    record["exports"][0]["name"] = "../foreign"
    artifacts.record_path.write_text(json.dumps(record))
    with pytest.raises(CaptureError, match="names"):
        DurableCaptureArtifacts.load(staging, state, "a" * 64, "b" * 32).cleanup_success()
    assert outside.read_bytes() == b"keep"


def test_reviewed_pending_export_is_hashed_and_change_blocks_discard(tmp_path):
    staging = tmp_path / "staging"
    state = tmp_path / "state"
    staging.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    artifacts = DurableCaptureArtifacts(staging, state, "a" * 64, "b" * 32, "daily")
    artifacts.prepare()
    pending = artifacts.directory / "partial.dump"
    artifacts.begin_export(pending)
    pending.write_bytes(b"unfinished point in time")
    pending.chmod(0o600)
    artifacts.reviewed_failure("failed")
    reviewed = json.loads(artifacts.record_path.read_text())
    assert reviewed["pending_observed"] == [{
        "name": "partial.dump", "size": pending.stat().st_size,
        "sha256": hashlib.sha256(pending.read_bytes()).hexdigest()}]
    digest = artifacts.record_sha256()
    pending.write_bytes(b"changed but still retained")
    with pytest.raises(CaptureError, match="changed"):
        artifacts.discard(digest)
    assert pending.exists()


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
