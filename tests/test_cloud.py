from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path

import pytest

from bbackup.cloud import CloudError, S3CloudAdapter


SOURCE_REPOSITORY = "local"
REPOSITORY_ID = "d" * 64
SOURCE_SNAPSHOT = "a" * 64
CAPTURED_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeS3:
    def __init__(self, pages=None, *, versioning="Enabled", rules=None, objects=None):
        self.pages = pages or {}
        self.versioning = versioning
        self.rules = (
            rules
            if rules is not None
            else [
                {
                    "ID": "retain-noncurrent",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "repo/"},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                }
            ]
        )
        self.objects = objects or {}
        self.list_calls = []
        self.get_calls = []
        self.returned_version_id = None

    def get_bucket_versioning(self, **_kwargs):
        return {"Status": self.versioning} if self.versioning else {}

    def get_bucket_lifecycle_configuration(self, **_kwargs):
        return {"Rules": self.rules}

    def list_object_versions(self, **kwargs):
        self.list_calls.append(kwargs)
        marker = (kwargs.get("KeyMarker"), kwargs.get("VersionIdMarker"))
        return self.pages[marker]

    def get_object(self, **kwargs):
        self.get_calls.append(kwargs)
        value = self.objects[kwargs["Key"]]
        if isinstance(value, Exception):
            raise value
        version_id = self.returned_version_id or kwargs["VersionId"]
        return {
            "Body": value if hasattr(value, "read") else io.BytesIO(value),
            "VersionId": version_id,
        }


def _page_client(*, rules=None, objects=None):
    pages = {
        (None, None): {
            "Versions": [
                {
                    "Key": "repo/config",
                    "VersionId": "config-v2",
                    "IsLatest": True,
                    "Size": 4,
                },
                {
                    "Key": "repo/config",
                    "VersionId": "config-v1",
                    "IsLatest": False,
                    "Size": 3,
                },
                {
                    "Key": "repo/locks/transient",
                    "VersionId": "lock-v1",
                    "IsLatest": True,
                    "Size": 4,
                },
            ],
            "DeleteMarkers": [
                {
                    "Key": "repo/removed",
                    "VersionId": "removed-delete",
                    "IsLatest": True,
                },
            ],
            "IsTruncated": True,
            "NextKeyMarker": "repo/removed",
            "NextVersionIdMarker": "removed-delete",
        },
        ("repo/removed", "removed-delete"): {
            "Versions": [
                {
                    "Key": "repo/data/pack",
                    "VersionId": "pack-v7",
                    "IsLatest": True,
                    "Size": 5,
                },
            ],
            "DeleteMarkers": [],
            "IsTruncated": False,
        },
    }
    return FakeS3(pages, rules=rules, objects=objects)


def _adapter(client: FakeS3, *, page_size=2) -> S3CloudAdapter:
    return S3CloudAdapter(
        "test-bucket",
        client=client,
        endpoint_url="https://s3.test.invalid",
        prefix="repo",
        page_size=page_size,
    )


def _create(adapter: S3CloudAdapter, path: Path):
    return adapter.create_checkpoint(
        path,
        source_repository=SOURCE_REPOSITORY,
        repository_id=REPOSITORY_ID,
        source_snapshot_id=SOURCE_SNAPSHOT,
        trusted_quiescent_window_confirmed=True,
        captured_at=CAPTURED_AT,
    )


def _restore(adapter: S3CloudAdapter, manifest: Path, target: Path, digest: str):
    return adapter.restore_checkpoint(
        manifest,
        target,
        expected_manifest_sha256=digest,
        expected_source_repository=SOURCE_REPOSITORY,
        expected_repository_id=REPOSITORY_ID,
        expected_source_snapshot_id=SOURCE_SNAPSHOT,
    )


def test_checkpoint_streams_current_versions_markers_and_skips_restic_locks(tmp_path):
    client = _page_client()
    adapter = _adapter(client)
    report = adapter.inspect_lifecycle()
    manifest = tmp_path / "checkpoint.jsonl"

    record = _create(adapter, manifest)

    assert report.versioning_enabled
    assert report.minimum_noncurrent_days == 30
    assert report.protection_qualified is False
    assert record.protection_qualified is False
    assert record.item_count == 3
    assert record.object_count == 2
    assert record.delete_marker_count == 1
    assert record.expires_at == "2026-01-30T00:00:00Z"
    assert record.manifest_sha256 == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert all(
        call["MaxKeys"] == 2 and call["MaxKeys"] <= 1000 for call in client.list_calls
    )
    assert client.list_calls[1]["KeyMarker"] == "repo/removed"
    assert client.list_calls[1]["VersionIdMarker"] == "removed-delete"

    rows = [json.loads(line) for line in manifest.read_text().splitlines()]
    objects = [row for row in rows if row["type"] == "object"]
    markers = [row for row in rows if row["type"] == "delete_marker"]
    assert {(row["key"], row["version_id"]) for row in objects} == {
        ("repo/config", "config-v2"),
        ("repo/data/pack", "pack-v7"),
    }
    assert markers == [
        {"key": "repo/removed", "type": "delete_marker", "version_id": "removed-delete"}
    ]
    assert not any("locks/" in row.get("key", "") for row in rows)
    header = rows[0]
    assert header["repository_id"] == REPOSITORY_ID
    assert header["endpoint_url"] == "https://s3.test.invalid"
    assert header["source_snapshot_id"] == SOURCE_SNAPSHOT
    assert header["quiescent_window_confirmed"] is True
    assert header["protection_qualified"] is False
    assert header["copyright"] == "Copyright © 2026 Crux Experts LLC"
    assert header["copyright_url"] == "https://www.cruxexperts.com/"
    assert "Private asset." in header["private_notice"]


@pytest.mark.parametrize(
    ("rules", "versioning", "message"),
    [
        (
            [{"Status": "Enabled", "Expiration": {"Days": 45}}],
            "Enabled",
            "Current-version expiration",
        ),
        (
            [
                {
                    "Status": "Enabled",
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 29},
                }
            ],
            "Enabled",
            "at least 30 days",
        ),
        (
            [{"Status": "Enabled", "NoncurrentVersionExpiration": {}}],
            "Enabled",
            "not understood",
        ),
        (
            [
                {
                    "Status": "Enabled",
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                }
            ],
            None,
            "versioning must be enabled",
        ),
    ],
)
def test_checkpoint_rejects_unsafe_lifecycle_or_versioning(
    tmp_path, rules, versioning, message
):
    client = _page_client(rules=rules)
    client.versioning = versioning
    with pytest.raises(CloudError, match=message):
        _create(_adapter(client), tmp_path / "checkpoint.jsonl")
    assert client.list_calls == []


def test_checkpoint_requires_explicit_quiescent_window(tmp_path):
    client = _page_client()
    adapter = _adapter(client)
    with pytest.raises(CloudError, match="quiescent window"):
        adapter.create_checkpoint(
            tmp_path / "checkpoint.jsonl",
            source_repository=SOURCE_REPOSITORY,
            repository_id=REPOSITORY_ID,
            source_snapshot_id=SOURCE_SNAPSHOT,
            trusted_quiescent_window_confirmed=False,
        )
    assert client.list_calls == []


def test_checkpoint_rejects_duplicate_current_keys_and_stalled_markers(tmp_path):
    duplicate = FakeS3(
        {
            (None, None): {
                "Versions": [
                    {"Key": "repo/key", "VersionId": "v1", "IsLatest": True, "Size": 1}
                ],
                "IsTruncated": True,
                "NextKeyMarker": "repo/key",
                "NextVersionIdMarker": "v1",
            },
            ("repo/key", "v1"): {
                "Versions": [
                    {"Key": "repo/key", "VersionId": "v2", "IsLatest": True, "Size": 1}
                ],
                "IsTruncated": False,
            },
        }
    )
    with pytest.raises(CloudError, match="duplicate current keys"):
        _create(_adapter(duplicate), tmp_path / "duplicate.jsonl")
    assert not (tmp_path / "duplicate.jsonl").exists()

    stalled = FakeS3(
        {
            (None, None): {
                "Versions": [],
                "IsTruncated": True,
                "NextKeyMarker": "repo/key",
                "NextVersionIdMarker": "v1",
            },
            ("repo/key", "v1"): {
                "Versions": [],
                "IsTruncated": True,
                "NextKeyMarker": "repo/key",
                "NextVersionIdMarker": "v1",
            },
        }
    )
    with pytest.raises(CloudError, match="did not progress"):
        _create(_adapter(stalled), tmp_path / "stalled.jsonl")


def test_restore_fetches_exact_object_versions_and_preserves_delete_markers(tmp_path):
    client = _page_client(
        objects={
            "repo/config": b"conf",
            "repo/data/pack": b"pack!",
        }
    )
    adapter = _adapter(client)
    manifest = tmp_path / "checkpoint.jsonl"
    record = _create(adapter, manifest)
    destination = tmp_path / "restored"
    destination.mkdir(mode=0o700)

    result = _restore(adapter, manifest, destination, record.manifest_sha256)

    assert result.files_restored == 2
    assert result.bytes_restored == 9
    assert result.delete_markers_preserved_as_absent == 1
    assert (destination / "config").read_bytes() == b"conf"
    assert (destination / "data" / "pack").read_bytes() == b"pack!"
    assert client.get_calls == [
        {"Bucket": "test-bucket", "Key": "repo/config", "VersionId": "config-v2"},
        {"Bucket": "test-bucket", "Key": "repo/data/pack", "VersionId": "pack-v7"},
    ]


def test_restore_pins_the_same_manifest_copy_it_hashes(tmp_path, monkeypatch):
    client = _page_client(objects={"repo/config": b"conf", "repo/data/pack": b"pack!"})
    adapter = _adapter(client)
    manifest = tmp_path / "checkpoint.jsonl"
    record = _create(adapter, manifest)
    trusted_bytes = manifest.read_bytes()
    destination = tmp_path / "restored"
    destination.mkdir()
    original_validate = adapter._validate_manifest

    def replace_original_before_validation(pinned_path, index_path, **kwargs):
        assert pinned_path != manifest
        manifest.write_bytes(
            manifest.read_bytes().replace(b"config-v2", b"attacker-v99")
        )
        return original_validate(pinned_path, index_path, **kwargs)

    monkeypatch.setattr(
        adapter, "_validate_manifest", replace_original_before_validation
    )

    result = _restore(adapter, manifest, destination, record.manifest_sha256)

    assert result.files_restored == 2
    assert client.get_calls[0]["VersionId"] == "config-v2"
    assert manifest.read_bytes() != trusted_bytes
    assert result.manifest_sha256 == record.manifest_sha256


def test_restore_rejects_checkpoint_bound_to_another_endpoint(tmp_path):
    client = _page_client(objects={"repo/config": b"conf", "repo/data/pack": b"pack!"})
    source_adapter = _adapter(client)
    manifest = tmp_path / "checkpoint.jsonl"
    record = _create(source_adapter, manifest)
    other_endpoint = S3CloudAdapter(
        "test-bucket",
        client=client,
        endpoint_url="https://another.test.invalid",
        prefix="repo",
    )
    destination = tmp_path / "restored"
    destination.mkdir()

    with pytest.raises(CloudError, match="different S3 endpoint"):
        other_endpoint.restore_checkpoint(
            manifest,
            destination,
            expected_manifest_sha256=record.manifest_sha256,
            expected_source_repository=SOURCE_REPOSITORY,
            expected_repository_id=REPOSITORY_ID,
            expected_source_snapshot_id=SOURCE_SNAPSHOT,
        )

    assert client.get_calls == []


def test_adapter_normalizes_and_rejects_unsafe_endpoint_urls():
    client = _page_client()
    adapter = S3CloudAdapter(
        "test-bucket", client=client, endpoint_url="HTTPS://S3.TEST.INVALID:443/"
    )
    assert adapter.endpoint_url == "https://s3.test.invalid"
    local = S3CloudAdapter(
        "test-bucket", client=client, endpoint_url="http://127.0.0.1:9000/"
    )
    assert local.endpoint_url == "http://127.0.0.1:9000"
    for endpoint in (
        "http://s3.test.invalid",
        "https://user:secret@s3.test.invalid",
        "https://s3.test.invalid?token=x",
        "https://s3.test.invalid#fragment",
    ):
        with pytest.raises(ValueError):
            S3CloudAdapter("test-bucket", client=client, endpoint_url=endpoint)


def test_restore_rejects_get_object_version_mismatch(tmp_path):
    client = _page_client(objects={"repo/config": b"conf", "repo/data/pack": b"pack!"})
    adapter = _adapter(client)
    manifest = tmp_path / "checkpoint.jsonl"
    record = _create(adapter, manifest)
    client.returned_version_id = "different-version"
    destination = tmp_path / "restored"
    destination.mkdir()

    with pytest.raises(CloudError, match="version does not match the checkpoint"):
        _restore(adapter, manifest, destination, record.manifest_sha256)

    assert list(destination.iterdir()) == []
    assert len(client.get_calls) == 1


def test_restore_caps_remote_body_to_declared_size(tmp_path):
    client = _page_client(
        objects={
            "repo/config": io.BytesIO(b"oversized payload"),
            "repo/data/pack": b"pack!",
        }
    )
    adapter = _adapter(client)
    manifest = tmp_path / "checkpoint.jsonl"
    record = _create(adapter, manifest)
    destination = tmp_path / "restored"
    destination.mkdir()

    with pytest.raises(
        CloudError, match="exceeded checkpoint size; partial destination is preserved"
    ):
        _restore(adapter, manifest, destination, record.manifest_sha256)

    assert (destination / "config").stat().st_size == 0
    assert client.get_calls == [
        {"Bucket": "test-bucket", "Key": "repo/config", "VersionId": "config-v2"}
    ]


def test_restore_sanitizes_object_body_read_errors_and_preserves_partial_file(tmp_path):
    class FailingBody:
        def read(self, _size):
            raise OSError("sensitive endpoint detail")

        def close(self):
            pass

    client = _page_client(objects={"repo/config": FailingBody()})
    adapter = _adapter(client)
    manifest = tmp_path / "checkpoint.jsonl"
    record = _create(adapter, manifest)
    destination = tmp_path / "restored"
    destination.mkdir()

    with pytest.raises(
        CloudError, match="Could not read an exact checkpoint object version"
    ) as caught:
        _restore(adapter, manifest, destination, record.manifest_sha256)

    assert "sensitive endpoint detail" not in str(caught.value)
    assert (destination / "config").exists()
    assert (destination / "config").read_bytes() == b""


def test_restore_allows_expired_checkpoint_as_best_effort_and_reports_expiry(tmp_path):
    client = _page_client(objects={"repo/config": b"conf", "repo/data/pack": b"pack!"})
    adapter = _adapter(client)
    manifest = tmp_path / "expired.jsonl"
    expired_capture = datetime(2020, 1, 1, tzinfo=timezone.utc)
    record = adapter.create_checkpoint(
        manifest,
        source_repository=SOURCE_REPOSITORY,
        repository_id=REPOSITORY_ID,
        source_snapshot_id=SOURCE_SNAPSHOT,
        trusted_quiescent_window_confirmed=True,
        captured_at=expired_capture,
    )
    destination = tmp_path / "restored"
    destination.mkdir()

    result = _restore(adapter, manifest, destination, record.manifest_sha256)

    assert result.expires_at == "2020-01-30T00:00:00Z"
    assert result.protection_expired is True
    assert result.files_restored == 2


def test_restore_rejects_expiry_before_capture_time(tmp_path):
    client = _page_client()
    adapter = _adapter(client)
    manifest = tmp_path / "malformed-expiry.jsonl"
    _create(adapter, manifest)
    lines = manifest.read_bytes().splitlines(keepends=True)
    header = json.loads(lines[0])
    header["expires_at"] = "2025-12-31T00:00:00Z"
    lines[0] = (
        json.dumps(header, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    manifest.write_bytes(b"".join(lines))
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    destination = tmp_path / "restored"
    destination.mkdir()

    with pytest.raises(CloudError, match="expiry must follow its capture time"):
        _restore(adapter, manifest, destination, digest)

    assert client.get_calls == []


def test_restore_rejects_manifest_key_traversal_before_fetch(tmp_path):
    header = {
        "type": "checkpoint",
        "schema_version": 1,
        "provider": "s3-compatible-unqualified",
        "endpoint_url": "https://s3.test.invalid",
        "bucket": "test-bucket",
        "prefix": "repo/",
        "captured_at": "2026-01-01T00:00:00Z",
        "expires_at": "2026-01-30T00:00:00Z",
        "source_repository": SOURCE_REPOSITORY,
        "repository_id": REPOSITORY_ID,
        "source_snapshot_id": SOURCE_SNAPSHOT,
        "quiescent_window_confirmed": True,
        "protection_qualified": False,
        "copyright": "Copyright © 2026 Crux Experts LLC",
        "copyright_url": "https://www.cruxexperts.com/",
        "private_notice": "Private asset. Property of Crux Experts LLC. Use, reproduction, modification, distribution, or publication without prior written permission is prohibited.",
    }
    rows = [
        header,
        {"type": "object", "key": "repo/../outside", "version_id": "v1", "size": 1},
        {"type": "end", "item_count": 1, "object_count": 1, "delete_marker_count": 0},
    ]
    manifest = tmp_path / "malicious.jsonl"
    manifest.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in rows
        )
    )
    digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
    client = _page_client()
    adapter = _adapter(client)
    destination = tmp_path / "empty"
    destination.mkdir()

    with pytest.raises(CloudError, match="Unsafe object key"):
        _restore(adapter, manifest, destination, digest)

    assert client.get_calls == []
    assert list(destination.iterdir()) == []


def test_restore_requires_trusted_digest_and_empty_non_symlink_destination(tmp_path):
    client = _page_client(objects={"repo/config": b"conf", "repo/data/pack": b"pack!"})
    adapter = _adapter(client)
    manifest = tmp_path / "checkpoint.jsonl"
    record = _create(adapter, manifest)
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "existing").write_text("keep")
    with pytest.raises(CloudError, match="empty local directory"):
        _restore(adapter, manifest, occupied, record.manifest_sha256)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(CloudError, match="SHA256"):
        _restore(adapter, manifest, empty, "0" * 64)
    assert client.get_calls == []
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(CloudError, match="symlinks"):
        _restore(adapter, manifest, linked, record.manifest_sha256)


def test_restore_keeps_partial_files_when_later_version_fetch_fails(tmp_path):
    client = _page_client(
        objects={
            "repo/config": b"conf",
            "repo/data/pack": RuntimeError("unavailable"),
        }
    )
    adapter = _adapter(client)
    manifest = tmp_path / "checkpoint.jsonl"
    record = _create(adapter, manifest)
    destination = tmp_path / "restored"
    destination.mkdir()

    with pytest.raises(CloudError, match="partial destination"):
        _restore(adapter, manifest, destination, record.manifest_sha256)

    assert (destination / "config").read_bytes() == b"conf"
    assert not (destination / "data" / "pack").exists()


def test_manifest_preparation_is_bounded_and_cancellable(tmp_path, monkeypatch):
    from threading import Event
    import bbackup.cloud as cloud
    source = tmp_path / "large.jsonl"
    source.write_bytes(b"12345")
    cancel = Event()
    cancel.set()
    with pytest.raises(CloudError, match="cancelled"):
        S3CloudAdapter._pin_manifest(source, tmp_path / "cancelled", cancel)
    monkeypatch.setattr(cloud, "_MAX_MANIFEST_BYTES", 4)
    with pytest.raises(CloudError):
        S3CloudAdapter._pin_manifest(source, tmp_path / "oversized")
