import json
from types import SimpleNamespace
from threading import Event

import pytest

from bbackup.b2 import B2Error, B2VersionLockReader, inspect_b2, inspect_sdk
from bbackup.operations import OperationRunner


def _native_lock_api(info=None, *, allowed=None, endpoint="https://s3.us-west-004.backblazeb2.com", bucket_id="bucket-id-1"):
    auth = {
        "capabilities": ["listBuckets", "readFiles", "readFileRetentions", "readFileLegalHolds"],
        "buckets": [{"id": "bucket-id-1", "name": "backup-bucket"}],
        "namePrefix": "restic/",
    }
    if allowed:
        auth.update(allowed)
    if info is None:
        info = {
            "bucketId": "bucket-id-1", "fileId": "file-v1", "fileName": "restic/data", "action": "upload",
            "fileRetention": {"isClientAuthorizedToRead": True, "value": {"mode": None, "retainUntilTimestamp": None}},
            "legalHold": {"isClientAuthorizedToRead": True, "value": None},
        }
    bucket = SimpleNamespace(name="backup-bucket", id_=bucket_id)
    return SimpleNamespace(
        account_info=SimpleNamespace(
            get_allowed=lambda: auth,
            get_s3_api_url=lambda: endpoint,
            get_api_url=lambda: "https://api001.backblazeb2.com",
        ),
        list_buckets=lambda **kwargs: [bucket],
        session=SimpleNamespace(get_file_info_by_id=lambda file_id: info),
    )


def test_native_lock_reader_exact_authorized_null_and_future_protection():
    from datetime import datetime, timezone

    now = datetime(2026, 10, 20, tzinfo=timezone.utc)
    api_value = _native_lock_api()
    reader = B2VersionLockReader(
        "backup-bucket", "bucket-id-1", "restic/", "https://s3.us-west-004.backblazeb2.com",
        api=api_value, access_key_id="key", secret_access_key="secret",
    )
    evidence, held = reader.read("restic/data", "file-v1", now=now)
    assert evidence["provider"] == "b2-native" and evidence["legal_hold"] == "OFF"
    assert evidence["legal_hold_value"] is None
    assert evidence["mode"] is None and evidence["retain_until_timestamp_ms"] is None
    assert held is False
    api_value.session.get_file_info_by_id = lambda _id: {
        **_native_lock_api().session.get_file_info_by_id("file-v1"),
        "fileRetention": {"isClientAuthorizedToRead": True, "value": {
            "mode": "governance", "retainUntilTimestamp": int(now.timestamp() * 1000) + 600000,
        }},
    }
    assert reader.read("restic/data", "file-v1", now=now)[1] is True


@pytest.mark.parametrize("damage", [
    {"bucketId": "wrong"}, {"fileId": "wrong"}, {"fileName": "restic/other"}, {"action": "hide"},
    {"fileRetention": {"isClientAuthorizedToRead": False, "value": {"mode": None, "retainUntilTimestamp": None}}},
    {"legalHold": {"isClientAuthorizedToRead": False, "value": "off"}},
    {"fileRetention": {"isClientAuthorizedToRead": True, "value": {"mode": None, "retainUntilTimestamp": 0}}},
    {"legalHold": {"isClientAuthorizedToRead": True, "value": "unknown"}},
])
def test_native_lock_reader_rejects_ambiguous_exact_version(damage):
    from datetime import datetime, timezone

    info = dict(_native_lock_api().session.get_file_info_by_id("file-v1"))
    info.update(damage)
    reader = B2VersionLockReader(
        "backup-bucket", "bucket-id-1", "restic/", "https://s3.us-west-004.backblazeb2.com",
        api=_native_lock_api(info), access_key_id="key", secret_access_key="secret",
    )
    with pytest.raises(B2Error, match="unknown"):
        reader.read("restic/data", "file-v1", now=datetime.now(timezone.utc))


@pytest.mark.parametrize("kwargs", [
    {"bucket_id": "wrong"},
    {"endpoint": "https://s3.us-east-005.backblazeb2.com"},
    {"allowed": {"namePrefix": "restic/other/"}},
    {"allowed": {"capabilities": ["listBuckets", "readFiles"]}},
])
def test_native_lock_reader_rejects_scope_or_endpoint_mismatch(kwargs):
    with pytest.raises(B2Error, match="differs"):
        B2VersionLockReader(
            "backup-bucket", "bucket-id-1", "restic/", "https://s3.us-west-004.backblazeb2.com",
            api=_native_lock_api(**kwargs), access_key_id="key", secret_access_key="secret",
        )


class FakeBucket:
    name = "backup-bucket"
    id_ = "bucket-id-1"
    lifecycle_rules = [{"fileNamePrefix": "", "daysFromHidingToDeleting": 30}]

    def ls(self, **kwargs):
        assert kwargs == {
            "path": kwargs["path"],
            "latest_only": False,
            "recursive": True,
            "fetch_count": 1,
        }
        yield object(), None


def api(capabilities=None, rules=None, *, allowed=None, bucket_id="bucket-id-1"):
    caps = (
        ["listBuckets", "listFiles", "readFiles", "writeFiles"]
        if capabilities is None
        else capabilities
    )
    auth = {"capabilities": caps, "buckets": [{"id": "bucket-id-1", "name": "backup-bucket"}], "namePrefix": "restic/"}
    if allowed is not None:
        auth.update(allowed)
    info = SimpleNamespace(
        get_allowed=lambda: auth,
        get_s3_api_url=lambda: "https://s3.us-west-004.backblazeb2.com",
    )
    bucket = FakeBucket()
    bucket.id_ = bucket_id
    if rules is not None:
        bucket.lifecycle_rules = rules
    return SimpleNamespace(account_info=info, list_buckets=lambda **kwargs: [bucket])


def test_vendor_adapter_inspects_one_bucket_without_exposing_tokens():
    result = inspect_sdk("backup-bucket", prefix="restic/", api=api())
    assert result["version_listing_verified"]
    assert result["observed_noncurrent_days"] == 30
    assert result["credential_administration_denied"]
    assert result["protection_qualified"] is False
    profile = result["restricted_source_profile"]
    assert profile["version"] == 1
    assert profile["status"] == "eligible_from_observed_metadata"
    assert profile["basis"] == "sdk_authorization_metadata_only"
    assert profile["scope"]["key_bucket_matches_selected_bucket"]
    assert profile["scope"]["requested_prefix_within_machine_prefix"]
    assert profile["negative_test_proof"] == {
        "status": "not_performed",
        "permanent_deletion_denial_demonstrated": False,
        "administrative_denial_demonstrated": False,
    }
    assert not {"authorizationToken", "accountId", "applicationKey"} & result.keys()


def test_overlapping_lifecycle_and_dangerous_key_are_reported():
    result = inspect_sdk(
        "backup-bucket",
        prefix="restic/",
        api=api(
            ["listBuckets", "listFiles", "readFiles", "deleteFiles"],
            [
                {
                    "fileNamePrefix": "restic/sub/",
                    "daysFromUploadingToHiding": 90,
                    "daysFromHidingToDeleting": 7,
                }
            ],
        ),
    )
    assert result["unsafe_capabilities"] == ["deleteFiles"]
    assert result["observed_noncurrent_days"] == 7
    assert set(result["lifecycle_conflicts"]) == {
        "current_objects_expire",
        "noncurrent_window_under_30_days",
    }
    assert result["protection_qualified"] is False


def test_observed_profile_allows_bucket_reads_but_requires_write_files():
    result = inspect_sdk(
        "backup-bucket",
        prefix="restic/",
        api=api(["listBuckets", "listFiles", "readFiles", "readBuckets"]),
    )
    profile = result["restricted_source_profile"]
    assert result["version_listing_verified"]
    assert profile["status"] == "unqualified"
    assert profile["missing_required_capabilities"] == ["writeFiles"]
    assert "required_capabilities_missing" in profile["reasons"]
    assert "readBuckets" in profile["observed_capabilities"]


@pytest.mark.parametrize(
    ("allowed", "bucket_id", "prefix", "expected_reason"),
    [
        ({"buckets": None}, "bucket-id-1", "restic/", "key_not_bucket_bound"),
        ({"buckets": [{"id": "other-bucket-id", "name": "backup-bucket"}]}, "bucket-id-1", "restic/", "key_bucket_id_mismatch"),
        ({"buckets": [{"id": "bucket-id-1", "name": "backup-bucket"}, {"id": "second", "name": "another-bucket"}]}, "bucket-id-1", "restic/", "key_not_bucket_bound"),
        ({}, None, "restic/", "selected_bucket_id_unavailable"),
        ({"namePrefix": ""}, "bucket-id-1", "restic/", "machine_prefix_restriction_missing"),
        ({"namePrefix": "restic/a/"}, "bucket-id-1", "restic/", "requested_prefix_outside_machine_prefix"),
        ({"namePrefix": "restic/a/"}, "bucket-id-1", "restic/ab/", "requested_prefix_outside_machine_prefix"),
        ({"namePrefix": "restic/a"}, "bucket-id-1", "restic/a/", "machine_prefix_not_canonical_directory"),
    ],
)
def test_unqualified_bucket_or_directory_scope_is_reported(allowed, bucket_id, prefix, expected_reason):
    auth = {"buckets": [{"id": "bucket-id-1", "name": "backup-bucket"}], "namePrefix": "restic/"}
    auth.update(allowed)
    result = inspect_sdk(
        "backup-bucket", prefix=prefix, api=api(allowed=auth, bucket_id=bucket_id)
    )
    profile = result["restricted_source_profile"]
    assert profile["status"] == "unqualified"
    assert expected_reason in profile["reasons"]
    if expected_reason == "requested_prefix_outside_machine_prefix":
        assert result["version_listing_verified"] is False


def test_administrative_and_unrecognized_capabilities_disqualify_observed_profile():
    result = inspect_sdk(
        "backup-bucket",
        prefix="restic/",
        api=api(
            ["listBuckets", "listFiles", "readFiles", "writeFiles", "writeKeys", "futurePermission"]
        ),
    )
    profile = result["restricted_source_profile"]
    assert profile["status"] == "unqualified"
    assert "administrative_capability_present" in profile["reasons"]
    assert "unrecognized_capability_present" in profile["reasons"]
    assert profile["negative_test_proof"]["administrative_denial_demonstrated"] is False


@pytest.mark.parametrize(
    "auth",
    [
        {"capabilities": ["listBuckets", 42]},
        {"buckets": "bucket-id-1"},
        {"buckets": [{"id": "bucket-id-1"}]},
        {"buckets": [None]},
        {"namePrefix": ["restic/"]},
        {"namePrefix": "restic/\0"},
    ],
)
def test_malformed_authorization_metadata_fails_closed_without_echoing_values(auth):
    secretish = "authorizationToken=do-not-expose"
    with pytest.raises(B2Error) as caught:
        inspect_sdk(
            "backup-bucket",
            prefix="restic/",
            api=api(allowed={**auth, "irrelevant": secretish}),
        )
    assert "do-not-expose" not in str(caught.value)


def test_inspection_rejects_missing_read_access_and_reports_wrong_prefix():
    with pytest.raises(B2Error):
        inspect_sdk("backup-bucket", prefix="restic/", api=api(["listFiles"]))
    result = inspect_sdk("backup-bucket", prefix="different/", api=api())
    profile = result["restricted_source_profile"]
    assert profile["status"] == "unqualified"
    assert "requested_prefix_outside_machine_prefix" in profile["reasons"]
    assert result["version_listing_verified"] is False


def test_sdk_errors_are_sanitized():
    client = api()

    def fail(**kwargs):
        raise RuntimeError("authorizationToken=do-not-expose")

    client.list_buckets = fail
    with pytest.raises(B2Error) as caught:
        inspect_sdk("backup-bucket", prefix="restic/", api=client)
    assert "do-not-expose" not in str(caught.value)


def test_unrestricted_sdk_authorization_retains_read_only_listing():
    from b2sdk.v3 import AbstractAccountInfo

    defaults = AbstractAccountInfo.DEFAULT_ALLOWED
    result = inspect_sdk("backup-bucket", prefix="restic/", api=api(allowed={
        "buckets": defaults["buckets"], "namePrefix": defaults["namePrefix"],
    }))
    assert result["version_listing_verified"] is True
    assert result["restricted_source_profile"]["status"] == "unqualified"


def test_real_child_missing_credentials_and_precancel(monkeypatch):
    monkeypatch.delenv("BBACKUP_TEST_B2_KEY_ID", raising=False)
    monkeypatch.delenv("BBACKUP_TEST_B2_KEY", raising=False)
    with pytest.raises(B2Error):
        inspect_b2(
            "backup-bucket",
            key_id_env="BBACKUP_TEST_B2_KEY_ID",
            key_env="BBACKUP_TEST_B2_KEY",
        )
    event = Event()
    event.set()
    with pytest.raises(B2Error):
        inspect_b2("backup-bucket", cancel=event, runner=OperationRunner())


@pytest.mark.parametrize("status", [429, 503])
def test_vendor_http_retry_uses_polite_backoff(monkeypatch, status):
    from b2sdk._internal.b2http import B2Http
    from b2sdk.v3 import B2HttpApiConfig
    import requests

    delays, attempts = [], []

    class Session:
        def request(self, *args, **kwargs):
            attempts.append(1)
            response = requests.Response()
            response.status_code = status if len(attempts) == 1 else 200
            response._content = json.dumps(
                {"status": status, "code": "too_many_requests", "message": "busy"}
                if len(attempts) == 1
                else {"ok": True}
            ).encode()
            response.headers["Retry-After"] = "3"
            return response

    monkeypatch.setattr("b2sdk._internal.b2http.time.sleep", delays.append)
    client = B2Http(
        B2HttpApiConfig(
            http_session_factory=Session,
            install_clock_skew_hook=False,
            decode_content=True,
        )
    )
    assert client.post_json_return_json(
        "https://test.invalid", {}, {}, try_count=3
    ) == {"ok": True}
    assert len(attempts) == 2
    assert delays == ([3] if status == 429 else [1.0])
