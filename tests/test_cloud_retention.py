from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from datetime import datetime as RealDateTime, timedelta, timezone
from pathlib import Path

import pytest

import bbackup.cloud as cloud
from bbackup.cloud import CloudError, S3CloudAdapter
from bbackup.b2 import B2VersionLockReader
from bbackup.operations import OperationLedger, OperationPlan, OperationStatus, RepositoryConflictError, repository_key
from bbackup.recovery import verify_checkpoint as real_verify_checkpoint, verify_recovery_acceptance as real_verify_acceptance


REPOSITORY_ID = "d" * 64
VERIFIER = "F" * 40
SOURCE_SIGNER = "E" * 40
ENDPOINT = "https://retention.example.test"
BUCKET = "retention-fixture"
PREFIX = "repo/"


class FrozenDateTime(RealDateTime):
    current = RealDateTime(2026, 10, 20, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        value = cls.current
        return value if tz is None else value.astimezone(tz)


class RetentionStore:
    def __init__(self):
        self.versions = [
            ("repo/data/restore.bin", "historical-v1", False, 8),
            ("repo/data/restore.bin", "current-v2", True, 11),
            ("repo/other/old.bin", "old-v1", False, 4),
            ("repo/other/old.bin", "current-v2", True, 5),
        ]
        self.markers = [("repo/markers/removed.bin", "delete-v2", True)]
        self.rules = []
        self.legal_holds = {}
        self.retention = {}
        self.lock_error = False
        self.lifecycle_error = False
        self.deleted = []
        self.delete_mode = "success"
        self.bodies = {("repo/data/restore.bin", "historical-v1"): b"historic"}
        self.request_id = 0

    def _metadata(self):
        self.request_id += 1
        return {"ResponseMetadata": {"RequestId": f"request-{self.request_id}"}}

    def get_bucket_versioning(self, **_kwargs):
        return {"Status": "Enabled", "MFADelete": "Disabled", **self._metadata()}

    def get_bucket_lifecycle_configuration(self, **_kwargs):
        if self.lifecycle_error:
            raise RuntimeError("sensitive lifecycle service detail")
        return {"Rules": json.loads(json.dumps(self.rules)), **self._metadata()}

    def get_object_lock_configuration(self, **_kwargs):
        return {
            "ObjectLockConfiguration": {"ObjectLockEnabled": "Enabled"},
            **self._metadata(),
        }

    def list_object_versions(self, **kwargs):
        prefix = kwargs["Prefix"]
        versions = [
            {"Key": key, "VersionId": version, "IsLatest": latest, "Size": size,
             "LastModified": RealDateTime(2001, 1, 1, tzinfo=timezone.utc)}
            for key, version, latest, size in self.versions
            if key.startswith(prefix)
        ]
        markers = [
            {"Key": key, "VersionId": version, "IsLatest": latest}
            for key, version, latest in self.markers
            if key.startswith(prefix)
        ]
        return {"Versions": versions, "DeleteMarkers": markers, "IsTruncated": False}

    def get_object_legal_hold(self, **kwargs):
        if self.lock_error:
            raise RuntimeError("sensitive lock detail")
        key = (kwargs["Key"], kwargs["VersionId"])
        return {"LegalHold": {"Status": self.legal_holds.get(key, "OFF")}}

    def get_object_retention(self, **kwargs):
        if self.lock_error:
            raise RuntimeError("sensitive retention detail")
        key = (kwargs["Key"], kwargs["VersionId"])
        return {"Retention": dict(self.retention.get(key, {}))}

    def delete_object(self, **kwargs):
        assert set(kwargs) == {"Bucket", "Key", "VersionId"}
        self.deleted.append((kwargs["Key"], kwargs["VersionId"]))
        if self.delete_mode == "lose_before":
            raise TimeoutError("sensitive connection detail")
        self.versions = [
            row for row in self.versions
            if (row[0], row[1]) != (kwargs["Key"], kwargs["VersionId"])
        ]
        if self.delete_mode == "lose_after":
            raise TimeoutError("sensitive connection detail")
        return {"VersionId": kwargs["VersionId"]}

    def get_object(self, **kwargs):
        import io

        return {
            "VersionId": kwargs["VersionId"],
            "Body": io.BytesIO(self.bodies[(kwargs["Key"], kwargs["VersionId"])]),
        }


def _write_json(path: Path, value: dict):
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")
    path.chmod(0o600)


def _make_manifest(path: Path, snapshot: str, objects: list[tuple[str, str, bytes]]):
    captured = (FrozenDateTime.current - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    rows = [
        {
            "type": "checkpoint",
            "schema_version": 1,
            "provider": "s3-compatible-unqualified",
            "endpoint_url": ENDPOINT,
            "bucket": BUCKET,
            "prefix": PREFIX,
            "captured_at": captured,
            "expires_at": None,
            "source_repository": "local",
            "repository_id": REPOSITORY_ID,
            "source_snapshot_id": snapshot,
            "quiescent_window_confirmed": True,
            "protection_qualified": False,
            "copyright": cloud._COPYRIGHT,
            "copyright_url": cloud._COPYRIGHT_URL,
            "private_notice": cloud._PRIVATE_NOTICE,
        }
    ]
    rows.extend(
        {"type": "object", "key": key, "version_id": version, "size": len(body)}
        for key, version, body in objects
    )
    rows.append(
        {
            "type": "end",
            "item_count": len(objects),
            "object_count": len(objects),
            "delete_marker_count": 0,
        }
    )
    data = b"".join(cloud._json_line(row) for row in rows)
    path.write_bytes(data)
    path.chmod(0o600)
    return hashlib.sha256(data).hexdigest(), captured


def _make_record(root: Path, snapshot: str, objects: list[tuple[str, str, bytes]]):
    root.mkdir(mode=0o700)
    manifest = root / "checkpoint.jsonl"
    manifest_digest, captured_at = _make_manifest(manifest, snapshot, objects)
    verified_at = (FrozenDateTime.current - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    acceptance = {
        "type": "recovery_acceptance",
        "schema_version": 1,
        "manifest_sha256": manifest_digest,
        "source_signer": SOURCE_SIGNER,
        "verifier": VERIFIER,
        "source_repository": "local",
        "repository_id": REPOSITORY_ID,
        "snapshot_id": snapshot,
        "captured_at": captured_at,
        "expires_at": None,
        "verified_at": verified_at,
        "files_restored": len(objects),
        "bytes_restored": sum(len(body) for _, _, body in objects),
        "recovery_verified": True,
        "protection_qualified": False,
        "copyright": f"Copyright © {FrozenDateTime.current.year} Crux Experts LLC",
        "copyright_url": cloud._COPYRIGHT_URL,
        "private_notice": cloud._PRIVATE_NOTICE,
    }
    _write_json(root / "acceptance.json", acceptance)
    (root / "acceptance.sig").write_bytes(b"fixture central signature")
    (root / "checkpoint.sig").write_bytes(b"fixture source signature")
    for name in ("acceptance.sig", "checkpoint.sig"):
        (root / name).chmod(0o600)
    return root, manifest, manifest_digest, acceptance


def _write_authority(root: Path, known_good_digest: str, *, max_objects=20, grant_expiry=None):
    state = root / "state"
    state.mkdir(mode=0o700)
    policy_path = root / "policy.json"
    policy = {
        "schema_version": 1,
        "policy_id": "retention-test",
        "catalog_id": "c" * 64,
        "revision": 1,
        "endpoint_url": ENDPOINT,
        "bucket": BUCKET,
        "prefix": PREFIX,
        "repository_id": REPOSITORY_ID,
        "trusted_verifier": VERIFIER,
        "retention_counts": {"daily": 1, "weekly": 0, "monthly": 0},
        "grace_seconds": 30 * 24 * 60 * 60,
        "plan_max_age_seconds": 90 * 24 * 60 * 60,
        "max_apply_objects": max_objects,
        "max_apply_bytes": 1024,
        "state_dir": str(state),
        "known_good_manifest_digests": [known_good_digest],
        "pending_manifest_digests": [],
    }
    _write_json(policy_path, policy)
    grant_path = root / "grant.json"
    expires = grant_expiry or FrozenDateTime.current + timedelta(days=90)
    grant = {
        "schema_version": 1,
        "policy_id": policy["policy_id"],
        "catalog_id": policy["catalog_id"],
        "policy_revision": policy["revision"],
        "policy_sha256": cloud._retention_digest(policy_path.read_bytes()),
        "endpoint_url": ENDPOINT,
        "bucket": BUCKET,
        "prefix": PREFIX,
        "repository_id": REPOSITORY_ID,
        "action": "delete_noncurrent_versions",
        "expires_at": expires.isoformat().replace("+00:00", "Z"),
        "max_apply_objects": max_objects,
        "max_apply_bytes": 1024,
        "stop_conditions": list(cloud._RETENTION_STOP_CONDITIONS),
    }
    _write_json(grant_path, grant)
    return policy_path, grant_path, policy, grant, state


@pytest.fixture
def retention_env(tmp_path, monkeypatch):
    FrozenDateTime.current = RealDateTime(2026, 10, 20, tzinfo=timezone.utc)
    monkeypatch.setattr(cloud, "datetime", FrozenDateTime)

    from bbackup import recovery

    def verify_acceptance(record_dir, *, trusted_verifier, repository_id, snapshot, **_kwargs):
        record = json.loads((Path(record_dir) / "acceptance.json").read_text())
        assert (Path(record_dir) / "acceptance.sig").is_file()
        assert record["verifier"] == trusted_verifier
        assert record["repository_id"] == repository_id
        assert record["snapshot_id"] == snapshot
        return record

    def verify_checkpoint(manifest, _signature, _signer, **_kwargs):
        return hashlib.sha256(Path(manifest).read_bytes()).hexdigest()

    monkeypatch.setattr(recovery, "verify_recovery_acceptance", verify_acceptance)
    monkeypatch.setattr(recovery, "verify_checkpoint", verify_checkpoint)

    store = RetentionStore()
    adapter = S3CloudAdapter(BUCKET, client=store, endpoint_url=ENDPOINT, prefix="repo")
    record_dir, manifest, manifest_digest, acceptance = _make_record(
        tmp_path / "accepted", "a" * 64,
        [("repo/data/restore.bin", "historical-v1", b"historic")],
    )
    policy_path, grant_path, policy, grant, state = _write_authority(tmp_path, manifest_digest)
    adapter.initialize_central_retention_catalog(policy_path, grant_path)
    ledger = OperationLedger(state / "operations.sqlite3")
    return {
        "root": tmp_path,
        "store": store,
        "adapter": adapter,
        "record_dir": record_dir,
        "manifest": manifest,
        "manifest_digest": manifest_digest,
        "acceptance": acceptance,
        "policy_path": policy_path,
        "grant_path": grant_path,
        "policy": policy,
        "grant": grant,
        "state": state,
        "ledger": ledger,
        "repository_lease_id": repository_key("fixture-repository-binding"),
    }


def _plan(env, record_dirs=None):
    return env["adapter"].plan_central_retention(
        env["policy_path"],
        env["grant_path"],
        record_dirs if record_dirs is not None else [env["record_dir"]],
        quiescent_window_confirmed=True,
        repository_lease_id=env["repository_lease_id"],
    )


def _seed_and_age(env):
    first = _plan(env)
    assert first["candidate_count"] == 0
    catalog = json.loads((env["state"] / "retention-catalog.json").read_text())
    old_token = cloud._retention_version_token("repo/other/old.bin", "old-v1")
    assert catalog["first_observed_noncurrent"][old_token] == cloud._iso(FrozenDateTime.current)
    FrozenDateTime.current += timedelta(days=31)
    return _plan(env)


def _apply(env, plan, *, mutation_client=None, cancel_event=None):
    return env["adapter"].apply_central_retention(
        plan["operation_id"],
        plan["plan_sha256"],
        env["policy_path"],
        env["grant_path"],
        [env["record_dir"]],
        quiescent_window_confirmed=True,
        ledger=env["ledger"],
        cancel_event=cancel_event,
        mutation_client=mutation_client or env["store"],
    )


def _refresh_grant_for_policy(env):
    env["grant"]["policy_sha256"] = cloud._retention_digest(env["policy_path"].read_bytes())
    _write_json(env["grant_path"], env["grant"])


def _assert_repository_reacquirable(env):
    plan = OperationPlan(
        operation_id=uuid.uuid4().hex,
        repository_id=env["repository_lease_id"],
        label="probe",
        argv=("probe",),
        destructive=False,
    )
    lease = env["ledger"].begin(plan)
    lease.finish(OperationStatus.SUCCEEDED, 0)
    lease.close()


def _native_reader_for_retention(env):
    """An exact metadata fake; raw SDK parsing is covered in test_b2."""
    reader = object.__new__(B2VersionLockReader)
    reader.bucket = BUCKET
    reader.bucket_id = "bucket-id-1"
    reader.prefix = PREFIX
    reader.endpoint_url = ENDPOINT
    reader._credential_pair = ("key", "secret")
    reader.locks = {}

    def read(key, file_id, *, now):
        state = reader.locks.get((key, file_id), "none")
        if state == "error":
            raise RuntimeError("sensitive native provider detail")
        evidence = {
            "provider": "b2-native", "bucket": BUCKET, "bucket_id": reader.bucket_id,
            "key": key, "file_id": file_id, "action": "upload", "legal_hold": "ON" if state == "hold" else "OFF",
            "legal_hold_value": "on" if state == "hold" else None,
            "mode": "governance" if state == "future" else None,
            "retain_until_timestamp_ms": int((now + timedelta(minutes=10)).timestamp() * 1000) if state == "future" else None,
        }
        return evidence, state in {"hold", "future"}

    reader.read = read
    env["adapter"].native_lock_reader = reader
    env["adapter"]._credential_pair = ("key", "secret")
    env["policy"].update({"schema_version": 2, "lock_metadata": {"provider": "b2-native", "bucket_id": reader.bucket_id}})
    _write_json(env["policy_path"], env["policy"])
    _refresh_grant_for_policy(env)
    return reader


def test_native_schema2_null_can_age_and_lock_change_stops_predelete(retention_env):
    env = retention_env
    reader = _native_reader_for_retention(env)
    plan = _seed_and_age(env)
    assert plan["candidate_count"] == 1
    candidate = ("repo/other/old.bin", "old-v1")
    reader.locks[candidate] = "hold"
    with pytest.raises(CloudError):
        _apply(env, plan)
    assert env["store"].deleted == []
    reader.locks[candidate] = "error"
    with pytest.raises(CloudError, match="native per-version lock evidence is unknown"):
        cloud._CentralRetention(env["adapter"])._version_lock(
            *candidate, lock_enabled=False, now=FrozenDateTime.current,
        )
    assert env["store"].deleted == []


@pytest.mark.parametrize("state", ["hold", "future"])
def test_native_schema2_protected_versions_are_not_candidates(retention_env, state):
    env = retention_env
    reader = _native_reader_for_retention(env)
    _plan(env)
    FrozenDateTime.current += timedelta(days=31)
    reader.locks[("repo/other/old.bin", "old-v1")] = state
    assert _plan(env)["candidate_count"] == 0


def test_native_schema2_strict_policy_binding_and_schema1_compatibility(retention_env):
    env = retention_env
    assert cloud._validate_retention_policy(env["policy"], env["adapter"])[0]["schema_version"] == 1
    reader = _native_reader_for_retention(env)
    assert cloud._validate_retention_policy(env["policy"], env["adapter"])[0]["schema_version"] == 2
    for metadata in (
        {"provider": "b2-native", "bucket_id": "wrong"},
        {"provider": "s3", "bucket_id": reader.bucket_id},
        {"provider": "b2-native", "bucket_id": reader.bucket_id, "extra": True},
    ):
        with pytest.raises(CloudError, match="Native lock reader"):
            cloud._validate_retention_policy({**env["policy"], "lock_metadata": metadata}, env["adapter"])
    with pytest.raises(CloudError, match="Schema-1"):
        cloud._validate_retention_policy({key: value for key, value in env["policy"].items() if key != "lock_metadata"} | {"schema_version": 1}, env["adapter"])
    with pytest.raises(ValueError, match="credentials differ"):
        S3CloudAdapter(
            BUCKET, client=env["store"], endpoint_url=ENDPOINT, prefix="repo",
            native_lock_reader=reader, credential_pair=("other", "secret"),
        )


def test_schema1_generic_s3_lock_404_is_still_unknown(retention_env):
    env = retention_env
    class Ambiguous404(Exception):
        response = {"Error": {"Code": "404"}}

    def unknown(**_kwargs):
        raise Ambiguous404()

    env["store"].get_object_legal_hold = unknown
    with pytest.raises(CloudError, match="lock evidence is unknown"):
        cloud._CentralRetention(env["adapter"])._version_lock(
            "repo/other/old.bin", "old-v1", lock_enabled=True,
            now=FrozenDateTime.current,
        )


def test_schema2_single_attempt_delete_client_uses_frozen_pair(retention_env, monkeypatch):
    import boto3

    env = retention_env
    _native_reader_for_retention(env)
    calls = []
    expected = object()
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: calls.append((args, kwargs)) or expected)
    assert cloud._CentralRetention(env["adapter"])._single_attempt_mutation_client() is expected
    assert calls[0][0] == ("s3",)
    options = calls[0][1]
    assert options["aws_access_key_id"] == "key"
    assert options["aws_secret_access_key"] == "secret"
    assert options["config"].retries["total_max_attempts"] == 1


def test_plan_protects_historical_restore_latest_versions_and_delete_markers(retention_env):
    env = retention_env
    plan = _seed_and_age(env)
    plan_file = env["state"] / "retention-plans" / f"{plan['operation_id']}.json"
    private_plan = json.loads(plan_file.read_text())
    assert [(item["key"], item["version_id"]) for item in private_plan["candidates"]] == [
        ("repo/other/old.bin", "old-v1")
    ]
    assert plan_file.stat().st_mode & 0o777 == 0o400

    result = _apply(env, plan)

    assert result["status"] == "complete"
    assert env["store"].deleted == [("repo/other/old.bin", "old-v1")]
    assert ("repo/data/restore.bin", "historical-v1", False, 8) in env["store"].versions
    assert ("repo/data/restore.bin", "current-v2", True, 11) in env["store"].versions
    assert ("repo/other/old.bin", "current-v2", True, 5) in env["store"].versions
    assert env["store"].markers == [("repo/markers/removed.bin", "delete-v2", True)]

    restored = env["root"] / "restored"
    restored.mkdir()
    result = env["adapter"].restore_checkpoint(
        env["manifest"], restored,
        expected_manifest_sha256=env["manifest_digest"],
        expected_source_repository="local",
        expected_repository_id=REPOSITORY_ID,
        expected_source_snapshot_id="a" * 64,
    )
    assert result.files_restored == 1
    assert (restored / "data" / "restore.bin").read_bytes() == b"historic"


def test_recently_hidden_old_upload_starts_grace_now_and_last_modified_is_ignored(retention_env):
    env = retention_env
    plan = _plan(env)
    assert plan["candidate_count"] == 0
    catalog = json.loads((env["state"] / "retention-catalog.json").read_text())
    token = cloud._retention_version_token("repo/other/old.bin", "old-v1")
    assert catalog["first_observed_noncurrent"][token] == cloud._iso(FrozenDateTime.current)
    assert env["store"].deleted == []


def test_missing_catalog_record_and_reaccepted_timestamp_fail_closed(retention_env):
    env = retention_env
    second_dir, _second_manifest, _second_digest, _second_acceptance = _make_record(
        env["root"] / "second-accepted", "b" * 64,
        [("repo/other/old.bin", "old-v1", b"old!")],
    )
    _plan(env, [env["record_dir"], second_dir])
    with pytest.raises(CloudError, match="previously accepted"):
        _plan(env)

    acceptance = json.loads((env["record_dir"] / "acceptance.json").read_text())
    acceptance["verified_at"] = (FrozenDateTime.current - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    _write_json(env["record_dir"] / "acceptance.json", acceptance)
    with pytest.raises(CloudError, match="replaced or re-accepted"):
        _plan(env, [env["record_dir"], second_dir])


@pytest.mark.parametrize("drift", ["policy", "grant", "inventory", "lifecycle", "lock"])
def test_apply_rejects_policy_grant_inventory_lifecycle_and_lock_drift(retention_env, drift):
    env = retention_env
    plan = _seed_and_age(env)
    if drift == "policy":
        policy = dict(env["policy"])
        policy["revision"] += 1
        _write_json(env["policy_path"], policy)
    elif drift == "grant":
        grant = dict(env["grant"])
        grant["expires_at"] = (FrozenDateTime.current + timedelta(days=80)).isoformat().replace("+00:00", "Z")
        _write_json(env["grant_path"], grant)
    elif drift == "inventory":
        env["store"].versions.append(("repo/other/new.bin", "new-v1", True, 2))
    elif drift == "lifecycle":
        env["store"].rules = [{"ID": "new-rule", "Status": "Disabled", "Expiration": {"Days": 1}}]
    elif drift == "lock":
        env["store"].legal_holds[("repo/other/old.bin", "old-v1")] = "ON"

    with pytest.raises(CloudError):
        _apply(env, plan)
    assert env["store"].deleted == []


def test_unknown_lock_and_enabled_expiration_rules_block_planning(retention_env):
    env = retention_env
    _plan(env)
    FrozenDateTime.current += timedelta(days=31)
    env["store"].lock_error = True
    with pytest.raises(CloudError, match="lock evidence is unknown") as caught:
        _plan(env)
    assert "sensitive" not in str(caught.value)

    env["store"].lock_error = False
    env["store"].rules = [{"ID": "expire", "Status": "Enabled", "Expiration": {"Days": 1}}]
    with pytest.raises(CloudError, match="expiration blocks"):
        _plan(env)


def test_expired_and_wrong_target_grants_are_rejected(retention_env):
    env = retention_env
    grant = dict(env["grant"])
    grant["expires_at"] = (FrozenDateTime.current - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    _write_json(env["grant_path"], grant)
    with pytest.raises(CloudError, match="expired"):
        _plan(env)

    grant = dict(env["grant"])
    grant["bucket"] = "different-bucket"
    _write_json(env["grant_path"], grant)
    with pytest.raises(CloudError, match="does not match"):
        _plan(env)


def test_apply_bounds_reject_candidate_set_that_exceeds_authorized_limit(retention_env):
    env = retention_env
    env["store"].versions.extend(
        [
            ("repo/other2/old.bin", "old-v1", False, 4),
            ("repo/other2/old.bin", "current-v2", True, 5),
        ]
    )
    policy = dict(env["policy"])
    policy["max_apply_objects"] = 1
    _write_json(env["policy_path"], policy)
    grant = dict(env["grant"])
    grant["max_apply_objects"] = 1
    grant["policy_sha256"] = cloud._retention_digest(env["policy_path"].read_bytes())
    _write_json(env["grant_path"], grant)
    env["policy"] = policy
    env["grant"] = grant
    _plan(env)
    FrozenDateTime.current += timedelta(days=31)
    with pytest.raises(CloudError, match="authorized apply bounds"):
        _plan(env)
    assert env["store"].deleted == []


def test_expired_grant_after_plan_prevents_any_mutation(retention_env):
    env = retention_env
    grant = dict(env["grant"])
    grant["expires_at"] = (FrozenDateTime.current + timedelta(days=45)).isoformat().replace("+00:00", "Z")
    grant["policy_sha256"] = cloud._retention_digest(env["policy_path"].read_bytes())
    _write_json(env["grant_path"], grant)
    env["grant"] = grant
    _plan(env)
    FrozenDateTime.current += timedelta(days=31)
    plan = _plan(env)
    FrozenDateTime.current += timedelta(days=15)
    with pytest.raises(CloudError, match="expired"):
        _apply(env, plan)
    assert env["store"].deleted == []


def test_lost_delete_response_is_reconciled_read_only_and_never_replayed(retention_env):
    env = retention_env
    plan = _seed_and_age(env)
    env["store"].delete_mode = "lose_before"
    result = _apply(env, plan)
    assert result["status"] == "uncertain"
    assert len(env["store"].deleted) == 1
    assert ("repo/other/old.bin", "old-v1", False, 4) in env["store"].versions
    probe_fd = env["ledger"]._lock(env["repository_lease_id"])
    env["ledger"]._unlock(probe_fd)

    plan_path = env["state"] / "retention-plans" / f"{plan['operation_id']}.json"
    reconciled = env["adapter"].reconcile_central_retention(plan_path, ledger=env["ledger"])
    assert reconciled == {
        "status": "reconciled_abandoned",
        "operation_id": plan["operation_id"],
        "absent_count": 0,
        "present_count": 1,
        "unknown_count": 0,
    }
    with pytest.raises(CloudError, match="already applied"):
        _apply(env, plan)
    assert len(env["store"].deleted) == 1


def test_lost_response_after_exact_delete_reconciles_observed_absence(retention_env):
    env = retention_env
    plan = _seed_and_age(env)
    env["store"].delete_mode = "lose_after"
    result = _apply(env, plan)
    assert result["status"] == "uncertain"
    assert not any(key == "repo/other/old.bin" and version == "old-v1" for key, version, *_ in env["store"].versions)

    plan_path = env["state"] / "retention-plans" / f"{plan['operation_id']}.json"
    reconciled = env["adapter"].reconcile_central_retention(plan_path, ledger=env["ledger"])
    assert reconciled["status"] == "reconciled_succeeded"
    assert reconciled["absent_count"] == 1
    assert len(env["store"].deleted) == 1


def test_catalog_loss_cannot_reset_accepted_history(retention_env):
    env = retention_env
    _plan(env)
    (env["state"] / "retention-catalog.json").unlink()
    with pytest.raises(CloudError, match="catalog is missing"):
        _plan(env)
    with pytest.raises(CloudError, match="history exists"):
        env["adapter"].initialize_central_retention_catalog(env["policy_path"], env["grant_path"])


def test_known_good_pin_required_even_with_period_counts(retention_env):
    env = retention_env
    env["policy"]["known_good_manifest_digests"] = []
    _write_json(env["policy_path"], env["policy"])
    _refresh_grant_for_policy(env)
    with pytest.raises(CloudError, match="known-good"):
        _plan(env)


def test_newly_accepted_unselected_checkpoint_protects_aged_version(retention_env):
    env = retention_env
    aged = _seed_and_age(env)
    assert aged["candidate_count"] == 1
    second_dir, _manifest, _digest, _acceptance = _make_record(
        env["root"] / "new-acceptance", "b" * 64,
        [("repo/other/old.bin", "old-v1", b"old!")],
    )
    env["policy"]["retention_counts"] = {"daily": 0, "weekly": 0, "monthly": 0}
    _write_json(env["policy_path"], env["policy"])
    _refresh_grant_for_policy(env)
    plan = _plan(env, [env["record_dir"], second_dir])
    assert plan["candidate_count"] == 0


@pytest.mark.parametrize("damage", ["missing", "wrong_size"])
def test_missing_or_changed_known_good_version_blocks_plan_and_apply(retention_env, damage):
    env = retention_env
    reviewed = _seed_and_age(env)
    assert reviewed["candidate_count"] == 1
    previous_plans = set((env["state"] / "retention-plans").iterdir())
    if damage == "missing":
        env["store"].versions = [
            row for row in env["store"].versions
            if (row[0], row[1]) != ("repo/data/restore.bin", "historical-v1")
        ]
    else:
        env["store"].versions = [
            (key, version, latest, 9 if version == "historical-v1" else size)
            for key, version, latest, size in env["store"].versions
        ]
    with pytest.raises(CloudError, match="protected recovery checkpoint version"):
        _plan(env)
    with pytest.raises(CloudError, match="protected recovery checkpoint version"):
        _apply(env, reviewed)
    assert set((env["state"] / "retention-plans").iterdir()) == previous_plans
    assert not (env["state"] / "retention-operations" / f"{reviewed['operation_id']}.json").exists()
    assert env["store"].deleted == []


@pytest.mark.parametrize("outcome", ["complete", "stopped", "cancelled"])
def test_definite_apply_outcomes_release_repository_lease(retention_env, outcome):
    from threading import Event

    env = retention_env
    plan = _seed_and_age(env)
    if outcome == "stopped":
        original_hold = env["store"].get_object_legal_hold
        checks = 0

        def newly_held(**kwargs):
            nonlocal checks
            checks += 1
            if checks >= 2:
                env["store"].legal_holds[("repo/other/old.bin", "old-v1")] = "ON"
            return original_hold(**kwargs)

        env["store"].get_object_legal_hold = newly_held
    cancellation = Event()
    if outcome == "cancelled":
        cancellation.set()
    result = env["adapter"].apply_central_retention(
        plan["operation_id"], plan["plan_sha256"], env["policy_path"],
        env["grant_path"], [env["record_dir"]],
        quiescent_window_confirmed=True, ledger=env["ledger"],
        cancel_event=cancellation, mutation_client=env["store"],
    )
    assert result["status"] == outcome
    _assert_repository_reacquirable(env)


def test_busy_repository_blocks_scan_and_journal(retention_env, monkeypatch):
    env = retention_env
    plan = _seed_and_age(env)
    holder = env["ledger"].begin(OperationPlan(
        operation_id=uuid.uuid4().hex, repository_id=env["repository_lease_id"],
        label="holder", argv=("hold",), destructive=True,
    ))
    try:
        monkeypatch.setattr(cloud._CentralRetention, "_plan_inputs", lambda *_a, **_k: pytest.fail("scan while busy"))
        with pytest.raises(RepositoryConflictError):
            _apply(env, plan)
        assert not (env["state"] / "retention-operations" / f"{plan['operation_id']}.json").exists()
        assert env["store"].deleted == []
    finally:
        holder.finish(OperationStatus.SUCCEEDED, 0)
        holder.close()


def test_apply_and_reconcile_require_expected_service_repository_lease(retention_env):
    env = retention_env
    plan = _seed_and_age(env)
    wrong = repository_key("different-service-repository")
    with pytest.raises(CloudError, match="different repository lease"):
        env["adapter"].apply_central_retention(
            plan["operation_id"], plan["plan_sha256"], env["policy_path"],
            env["grant_path"], [env["record_dir"]], quiescent_window_confirmed=True,
            ledger=env["ledger"], mutation_client=env["store"],
            expected_repository_lease_id=wrong,
        )
    assert env["store"].deleted == []
    assert not (env["state"] / "retention-operations" / f"{plan['operation_id']}.json").exists()
    assert _apply(env, plan)["status"] == "complete"
    plan_path = env["state"] / "retention-plans" / f"{plan['operation_id']}.json"
    with pytest.raises(CloudError, match="different repository lease"):
        env["adapter"].reconcile_central_retention(
            plan_path, ledger=env["ledger"], expected_repository_lease_id=wrong,
        )


def test_deadline_expiring_during_full_scan_fails_before_journal(retention_env, monkeypatch):
    env = retention_env
    env["policy"]["plan_max_age_seconds"] = 1
    _write_json(env["policy_path"], env["policy"])
    _refresh_grant_for_policy(env)
    plan = _seed_and_age(env)
    original = cloud._CentralRetention._inventory

    def slow_inventory(self, *args, **kwargs):
        value = original(self, *args, **kwargs)
        FrozenDateTime.current += timedelta(seconds=2)
        return value

    monkeypatch.setattr(cloud._CentralRetention, "_inventory", slow_inventory)
    with pytest.raises(CloudError, match="expired"):
        _apply(env, plan)
    assert env["store"].deleted == []
    assert not (env["state"] / "retention-operations" / f"{plan['operation_id']}.json").exists()
    _assert_repository_reacquirable(env)


def test_deadline_between_candidates_stops_additional_deletes(retention_env):
    env = retention_env
    env["store"].versions.extend([
        ("repo/other2/old.bin", "old-v1", False, 4),
        ("repo/other2/old.bin", "current-v2", True, 5),
    ])
    env["policy"]["plan_max_age_seconds"] = 1
    _write_json(env["policy_path"], env["policy"])
    _refresh_grant_for_policy(env)
    plan = _seed_and_age(env)
    assert plan["candidate_count"] == 2
    original = env["store"].delete_object

    def slow_delete(**kwargs):
        result = original(**kwargs)
        FrozenDateTime.current += timedelta(seconds=2)
        return result

    env["store"].delete_object = slow_delete
    result = _apply(env, plan)
    assert result["status"] == "stopped"
    assert len(env["store"].deleted) == 1
    _assert_repository_reacquirable(env)


@pytest.mark.parametrize("boundary", ["lock_read", "intent_saved"])
def test_cancellation_before_intent_or_delete_dispatches_nothing(retention_env, monkeypatch, boundary):
    from threading import Event

    env = retention_env
    plan = _seed_and_age(env)
    cancellation = Event()
    if boundary == "lock_read":
        original_lock = cloud._CentralRetention._version_lock
        reads = 0

        def cancel_on_lock(self, *args, **kwargs):
            nonlocal reads
            result = original_lock(self, *args, **kwargs)
            reads += 1
            if reads == 2:
                cancellation.set()
            return result

        monkeypatch.setattr(cloud._CentralRetention, "_version_lock", cancel_on_lock)
    else:
        original_save = cloud._CentralRetention._save_journal

        def cancel_after_intent(path, journal):
            original_save(path, journal)
            if journal["items"][0]["status"] == "intent":
                cancellation.set()

        monkeypatch.setattr(cloud._CentralRetention, "_save_journal", staticmethod(cancel_after_intent))
    result = _apply(env, plan, cancel_event=cancellation)
    assert result["status"] == "cancelled"
    assert result["deleted_count"] == 0
    assert env["store"].deleted == []
    journal = json.loads((env["state"] / "retention-operations" / f"{plan['operation_id']}.json").read_text())
    assert journal["status"] == "cancelled"
    assert journal["items"][0]["status"] == "pending"
    _assert_repository_reacquirable(env)


def test_real_signatures_authenticate_acceptance_and_manifest(retention_env, monkeypatch):
    if not shutil.which("gpg"):
        pytest.skip("requires gpg")
    env = retention_env
    home = env["root"] / "gnupg"
    home.mkdir(mode=0o700)
    base = ["gpg", "--homedir", str(home), "--batch", "--pinentry-mode", "loopback", "--passphrase", ""]
    subprocess.run([*base, "--quick-generate-key", "retention disposable test", "ed25519", "sign", "1d"], check=True, capture_output=True)
    keys = subprocess.run([*base, "--with-colons", "--list-secret-keys"], check=True, capture_output=True, text=True).stdout
    fingerprint = next(line.split(":")[9] for line in keys.splitlines() if line.startswith("fpr:"))
    subprocess.run([*base, "--quick-generate-key", "retention independent verifier", "ed25519", "sign", "1d"], check=True, capture_output=True)
    keys = subprocess.run([*base, "--with-colons", "--list-secret-keys"], check=True, capture_output=True, text=True).stdout
    verifier = [line.split(":")[9] for line in keys.splitlines() if line.startswith("fpr:")][-1]
    monkeypatch.setenv("GNUPGHOME", str(home))
    monkeypatch.setattr(__import__("bbackup.recovery", fromlist=["verify_checkpoint"]), "verify_checkpoint", real_verify_checkpoint)
    monkeypatch.setattr(__import__("bbackup.recovery", fromlist=["verify_recovery_acceptance"]), "verify_recovery_acceptance", real_verify_acceptance)
    acceptance = dict(env["acceptance"])
    acceptance["source_signer"] = fingerprint
    acceptance["verifier"] = verifier
    _write_json(env["record_dir"] / "acceptance.json", acceptance)
    for filename, signature, signer in (("checkpoint.jsonl", "checkpoint.sig", fingerprint),
                                        ("acceptance.json", "acceptance.sig", verifier)):
        subprocess.run([*base, "--local-user", signer, "--output", str(env["record_dir"] / signature),
                        "--yes", "--detach-sign", str(env["record_dir"] / filename)], check=True, capture_output=True)
    env["policy"]["trusted_verifier"] = verifier
    _write_json(env["policy_path"], env["policy"])
    _refresh_grant_for_policy(env)
    try:
        assert real_verify_acceptance(env["record_dir"], trusted_verifier=verifier,
                                      repository_id=REPOSITORY_ID, snapshot="a" * 64)
        assert real_verify_checkpoint(env["manifest"], env["record_dir"] / "checkpoint.sig", fingerprint)
        plan = _seed_and_age(env)
        assert plan["candidate_count"] == 1
        assert _apply(env, plan)["status"] == "complete"
    finally:
        subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"], capture_output=True)
