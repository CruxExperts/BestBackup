import json
from types import SimpleNamespace
from threading import Event

import pytest

from bbackup.b2 import B2Error, inspect_b2, inspect_sdk
from bbackup.operations import OperationRunner


class FakeBucket:
    name = "backup-bucket"
    lifecycle_rules = [{"fileNamePrefix": "", "daysFromHidingToDeleting": 30}]

    def ls(self, **kwargs):
        assert kwargs == {
            "path": "restic/",
            "latest_only": False,
            "recursive": True,
            "fetch_count": 1,
        }
        yield object(), None


def api(capabilities=None, rules=None):
    caps = capabilities or ["listBuckets", "listFiles", "readFiles", "writeFiles"]
    info = SimpleNamespace(
        get_allowed=lambda: {"capabilities": caps, "namePrefix": "restic/"},
        get_s3_api_url=lambda: "https://s3.us-west-004.backblazeb2.com",
    )
    bucket = FakeBucket()
    if rules is not None:
        bucket.lifecycle_rules = rules
    return SimpleNamespace(account_info=info, list_buckets=lambda **kwargs: [bucket])


def test_vendor_adapter_inspects_one_bucket_without_exposing_tokens():
    result = inspect_sdk("backup-bucket", prefix="restic/", api=api())
    assert result["version_listing_verified"]
    assert result["observed_noncurrent_days"] == 30
    assert result["credential_administration_denied"]
    assert result["protection_qualified"] is False
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


def test_inspection_rejects_missing_permissions_and_wrong_prefix():
    with pytest.raises(B2Error):
        inspect_sdk("backup-bucket", prefix="restic/", api=api(["listFiles"]))
    with pytest.raises(B2Error):
        inspect_sdk("backup-bucket", prefix="different/", api=api())


def test_sdk_errors_are_sanitized():
    client = api()

    def fail(**kwargs):
        raise RuntimeError("authorizationToken=do-not-expose")

    client.list_buckets = fail
    with pytest.raises(B2Error) as caught:
        inspect_sdk("backup-bucket", prefix="restic/", api=client)
    assert "do-not-expose" not in str(caught.value)


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
