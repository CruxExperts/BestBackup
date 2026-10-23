"""Small read-only B2 Native API adapter; restic owns backup transfers.

Tokens exist only in memory. Native inspection complements the shared S3
checkpoint path; it does not confer a tested recovery-protection claim.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlsplit


from .models import strict_json


class B2Error(RuntimeError):
    """Sanitized B2 connection, authorization, or inspection failure."""


_ADMIN = {
    "deleteFiles",
    "listKeys",
    "deleteKeys",
    "writeKeys",
    "writeBuckets",
    "deleteBuckets",
    "writeBucketEncryption",
    "writeBucketRetentions",
    "writeFileRetentions",
    "writeFileLegalHolds",
    "bypassGovernance",
    "writeBucketReplications",
    "writeBucketNotifications",
}
_SAFE_CAPABILITIES = {
    "listBuckets", "listAllBucketNames", "listFiles", "readFiles", "writeFiles",
    "readBuckets", "readBucketEncryption", "readBucketRetentions",
    "readFileRetentions", "readFileLegalHolds", "readBucketReplications",
    "readBucketNotifications", "readBucketLifecycleRules",
}
_INSPECTION_CAPABILITIES = {"listBuckets", "listFiles", "readFiles"}
_PROFILE_CAPABILITIES = _INSPECTION_CAPABILITIES | {"writeFiles"}
_LOCK_READ_CAPABILITIES = {
    "listBuckets", "readFiles", "readFileRetentions", "readFileLegalHolds",
}


def _restricted_source_profile(
    *, bucket_id, bucket_name, allowed_buckets, prefix, allowed_prefix, capabilities
):
    """Assess the observed key declaration without claiming live denial proof."""
    reasons = []
    bucket_bound = bool(allowed_buckets and len(allowed_buckets) == 1)
    bucket_matches = bool(
        bucket_bound and bucket_id == allowed_buckets[0]["id"]
        and bucket_name == allowed_buckets[0]["name"]
    )
    if not bucket_bound:
        reasons.append("key_not_bucket_bound")
    if not bucket_id:
        reasons.append("selected_bucket_id_unavailable")
    elif bucket_bound and not bucket_matches:
        reasons.append("key_bucket_id_mismatch")

    prefix_restricted = bool(allowed_prefix)
    prefix_canonical = bool(
        prefix_restricted and allowed_prefix.endswith("/") and "\0" not in allowed_prefix
    )
    prefix_contains_request = bool(
        prefix_canonical and prefix.startswith(allowed_prefix)
    )
    if not prefix_restricted:
        reasons.append("machine_prefix_restriction_missing")
    elif not prefix_canonical:
        reasons.append("machine_prefix_not_canonical_directory")
    elif not prefix_contains_request:
        reasons.append("requested_prefix_outside_machine_prefix")

    observed = set(capabilities)
    missing = sorted(_PROFILE_CAPABILITIES - observed)
    if missing:
        reasons.append("required_capabilities_missing")
    if "deleteFiles" in observed:
        reasons.append("permanent_deletion_capability_present")
    if observed & (_ADMIN - {"deleteFiles"}):
        reasons.append("administrative_capability_present")
    if observed - _ADMIN - _SAFE_CAPABILITIES:
        reasons.append("unrecognized_capability_present")

    return {
        "version": 1,
        "status": "eligible_from_observed_metadata" if not reasons else "unqualified",
        "basis": "sdk_authorization_metadata_only",
        "reasons": reasons,
        "required_capabilities": sorted(_PROFILE_CAPABILITIES),
        "observed_capabilities": sorted(observed),
        "missing_required_capabilities": missing,
        "scope": {
            "key_bucket_bound": bucket_bound,
            "key_bucket_matches_selected_bucket": bucket_matches,
            "machine_prefix_restricted": prefix_restricted,
            "machine_prefix_canonical_directory": prefix_canonical,
            "requested_prefix_within_machine_prefix": prefix_contains_request,
        },
        "negative_test_proof": {
            "status": "not_performed",
            "permanent_deletion_denial_demonstrated": False,
            "administrative_denial_demonstrated": False,
        },
    }


def _endpoint(value, pattern):
    if not isinstance(value, str):
        raise B2Error("B2 returned an invalid endpoint")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not re.fullmatch(pattern, parsed.netloc)
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise B2Error("B2 returned an untrusted endpoint")
    return value.rstrip("/")


class B2VersionLockReader:
    """Read one exact B2 native file version, retaining authorization flags.

    The application key is supplied explicitly and exists only in SDK memory.
    Neither normalized SDK FileVersion objects nor S3's ambiguous 404 responses
    can establish that an absent lock is safe for deletion.
    """

    def __init__(
        self, bucket: str, bucket_id: str, prefix: str, endpoint_url: str,
        *, access_key_id: str | None = None, secret_access_key: str | None = None,
        api=None,
    ) -> None:
        if (
            not isinstance(bucket, str) or not re.fullmatch(r"[a-zA-Z0-9-]{6,63}", bucket)
            or not isinstance(bucket_id, str) or not bucket_id or any(c in bucket_id for c in "\0\r\n")
            or not isinstance(prefix, str) or not prefix or not prefix.endswith("/")
            or prefix.startswith("/") or "\0" in prefix
        ):
            raise B2Error("Invalid B2 lock-reader target")
        endpoint = _endpoint(endpoint_url, r"s3\.[a-z0-9-]+\.backblazeb2\.com")
        if api is None:
            if not access_key_id or not secret_access_key:
                raise B2Error("Explicit B2 lock-reader credentials are required")
            try:
                from b2sdk.v3 import B2Api, InMemoryAccountInfo

                api = B2Api(
                    InMemoryAccountInfo(), max_upload_workers=1,
                    max_copy_workers=1, max_download_workers=1,
                )
                api.authorize_account(access_key_id, secret_access_key)
            except Exception:
                raise B2Error("Could not authorize B2 lock reader") from None
        try:
            observed_endpoint = _endpoint(
                api.account_info.get_s3_api_url(), r"s3\.[a-z0-9-]+\.backblazeb2\.com"
            )
            _endpoint(api.account_info.get_api_url(), r"api\d+\.backblazeb2\.com")
            allowed = api.account_info.get_allowed()
            if not isinstance(allowed, dict):
                raise ValueError()
            capabilities = allowed.get("capabilities")
            if not isinstance(capabilities, list) or not all(isinstance(c, str) for c in capabilities):
                raise ValueError()
            if not _LOCK_READ_CAPABILITIES <= set(capabilities):
                raise ValueError()
            allowed_buckets = allowed.get("buckets")
            if allowed_buckets is not None and (
                not isinstance(allowed_buckets, list)
                or not any(
                    isinstance(item, dict) and item.get("id") == bucket_id
                    and item.get("name") == bucket for item in allowed_buckets
                )
            ):
                raise ValueError()
            allowed_prefix = allowed.get("namePrefix")
            if allowed_prefix is not None and (
                not isinstance(allowed_prefix, str) or not prefix.startswith(allowed_prefix)
            ):
                raise ValueError()
            buckets = api.list_buckets(bucket_name=bucket)
            if (
                not isinstance(buckets, list) or len(buckets) != 1
                or buckets[0].name != bucket or buckets[0].id_ != bucket_id
                or observed_endpoint != endpoint
            ):
                raise ValueError()
        except Exception:
            raise B2Error("B2 lock-reader endpoint, authority, or bucket differs from target") from None
        self.bucket = bucket
        self.bucket_id = bucket_id
        self.prefix = prefix
        self.endpoint_url = endpoint
        self._api = api
        self._credential_pair = (access_key_id, secret_access_key) if access_key_id and secret_access_key else None

    def matches_credentials(self, pair: tuple[str, str]) -> bool:
        return self._credential_pair == pair

    def read(self, key: str, file_id: str, *, now) -> tuple[dict, bool]:
        if (
            not isinstance(key, str) or not key.startswith(self.prefix)
            or not isinstance(file_id, str) or not file_id or any(c in file_id for c in "\0\r\n")
        ):
            raise B2Error("B2 lock-reader version target is invalid")
        try:
            info = self._api.session.get_file_info_by_id(file_id)
            if not isinstance(info, dict) or (
                info.get("bucketId") != self.bucket_id or info.get("fileId") != file_id
                or info.get("fileName") != key or info.get("action") != "upload"
            ):
                raise ValueError()
            retention = info.get("fileRetention")
            legal = info.get("legalHold")
            if (
                not isinstance(retention, dict) or not isinstance(legal, dict)
                or retention.get("isClientAuthorizedToRead") is not True
                or legal.get("isClientAuthorizedToRead") is not True
                or "value" not in retention or "value" not in legal
            ):
                raise ValueError()
            setting = retention["value"]
            if not isinstance(setting, dict) or set(setting) != {"mode", "retainUntilTimestamp"}:
                raise ValueError()
            mode, until_ms = setting["mode"], setting["retainUntilTimestamp"]
            if mode is None:
                if until_ms is not None:
                    raise ValueError()
            elif mode not in {"governance", "compliance"} or type(until_ms) is not int or until_ms < 0:
                raise ValueError()
            legal_value = legal["value"]
            if legal_value not in ("on", "off", None):
                raise ValueError()
            from datetime import datetime, timezone

            until = datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc) if until_ms is not None else None
            if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError()
            evidence = {
                "provider": "b2-native", "bucket": self.bucket,
                "bucket_id": self.bucket_id, "key": key, "file_id": file_id,
                "action": "upload", "legal_hold": "ON" if legal_value == "on" else "OFF",
                "legal_hold_value": legal_value,
                "mode": mode, "retain_until_timestamp_ms": until_ms,
            }
            return evidence, legal_value == "on" or (until is not None and until > now)
        except Exception:
            raise B2Error("B2 native per-version lock evidence is unknown") from None


def inspect_sdk(
    bucket: str,
    *,
    prefix="",
    key_id_env="B2_APPLICATION_KEY_ID",
    key_env="B2_APPLICATION_KEY",
    api=None,
) -> dict:
    """Read-only SDK operation; production invokes this inside a supervised child."""
    if (
        not re.fullmatch(r"[a-zA-Z0-9-]{6,63}", bucket)
        or not isinstance(prefix, str)
        or "\0" in prefix
    ):
        raise B2Error("Invalid B2 bucket or prefix")
    for name in (key_id_env, key_env):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise B2Error("Invalid credential environment variable name")
    try:
        if api is None:
            from b2sdk.v3 import B2Api, InMemoryAccountInfo

            key_id, key = os.environ.get(key_id_env), os.environ.get(key_env)
            if not key_id or not key:
                raise B2Error("B2 application key environment variables are required")
            api = B2Api(
                InMemoryAccountInfo(),
                max_upload_workers=1,
                max_copy_workers=1,
                max_download_workers=1,
            )
            api.authorize_account(key_id, key)
        allowed = api.account_info.get_allowed()
        if not isinstance(allowed, dict):
            raise B2Error("B2 authorization metadata is invalid")
        capabilities = allowed.get("capabilities")
        if (
            not isinstance(capabilities, list)
            or any(not isinstance(c, str) or not c for c in capabilities)
        ):
            raise B2Error("B2 authorization metadata is invalid")
        if not _INSPECTION_CAPABILITIES.issubset(capabilities):
            raise B2Error(
                "B2 key needs listBuckets, listFiles, and readFiles for inspection"
            )
        # b2sdk v3 normalizes provider authorization into a list of buckets.
        # None means unrestricted; one matching id/name is the source profile.
        allowed_buckets = allowed.get("buckets")
        allowed_prefix = allowed.get("namePrefix")
        if (
            allowed_buckets is not None
            and (not isinstance(allowed_buckets, list) or any(
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str) or not item["id"]
                or not isinstance(item.get("name"), str) or not item["name"]
                for item in allowed_buckets
            ))
        ) or (
            allowed_prefix is not None
            and (not isinstance(allowed_prefix, str) or "\0" in allowed_prefix)
        ):
            raise B2Error("B2 authorization metadata is invalid")
        s3_url = _endpoint(
            api.account_info.get_s3_api_url(), r"s3\.[a-z0-9-]+\.backblazeb2\.com"
        )
        matches = api.list_buckets(bucket_name=bucket)
        if len(matches) != 1 or matches[0].name != bucket:
            raise B2Error("B2 bucket is missing or inaccessible")
        selected = matches[0]
        selected_bucket_id = getattr(selected, "id_", None)
        if selected_bucket_id is not None and not isinstance(selected_bucket_id, str):
            raise B2Error("B2 bucket identity metadata is invalid")
        profile = _restricted_source_profile(
            bucket_id=selected_bucket_id,
            bucket_name=bucket,
            allowed_buckets=allowed_buckets,
            prefix=prefix,
            allowed_prefix=allowed_prefix,
            capabilities=capabilities,
        )
        version_listing_verified = False
        # Inspection remains available to broader observer keys. The stricter
        # machine-directory assessment is separate from the actual API scope.
        if prefix.startswith(allowed_prefix or ""):
            versions = iter(
                selected.ls(
                    path=prefix, latest_only=False, recursive=True, fetch_count=1
                )
            )
            try:
                next(versions, None)
            finally:
                if hasattr(versions, "close"):
                    versions.close()
            version_listing_verified = True
        rules = selected.lifecycle_rules
        if not isinstance(rules, list):
            raise ValueError()
        conflicts, noncurrent_days = [], []
        for rule in rules:
            rule_prefix = rule["fileNamePrefix"]
            if not isinstance(rule_prefix, str):
                raise ValueError()
            if not (prefix.startswith(rule_prefix) or rule_prefix.startswith(prefix)):
                continue
            hide, expire = (
                rule.get("daysFromUploadingToHiding"),
                rule.get("daysFromHidingToDeleting"),
            )
            if hide is not None:
                conflicts.append("current_objects_expire")
            if expire is not None:
                if type(expire) is not int or expire < 30:
                    conflicts.append("noncurrent_window_under_30_days")
                if type(expire) is int and expire > 0:
                    noncurrent_days.append(expire)
        administrative = sorted(set(capabilities) & _ADMIN)
        if set(capabilities) - _ADMIN - _SAFE_CAPABILITIES:
            administrative.append("unrecognized_capabilities")
        return {
            "provider": "backblaze-b2",
            "sdk": "b2sdk",
            "bucket": bucket,
            "s3_endpoint": s3_url,
            "prefix": prefix,
            "bucket_access_verified": True,
            "version_listing_verified": version_listing_verified,
            "read_capability_present": True,
            "write_capability_present": "writeFiles" in capabilities,
            "credential_administration_denied": not administrative,
            "unsafe_capabilities": administrative,
            "lifecycle_conflicts": sorted(set(conflicts)),
            "observed_noncurrent_days": min(noncurrent_days)
            if noncurrent_days
            else None,
            "restricted_source_profile": profile,
            "protection_qualified": False,
            "live_recovery_demonstrated": False,
        }
    except B2Error:
        raise
    except Exception:
        # Vendor exceptions can include response bodies and credential-bearing
        # diagnostics. They are never propagated into public output or state.
        raise B2Error(
            "B2 SDK inspection failed; check credentials and bucket permissions"
        ) from None


def inspect_b2(
    bucket,
    *,
    prefix="",
    key_id_env="B2_APPLICATION_KEY_ID",
    key_env="B2_APPLICATION_KEY",
    cancel=None,
    runner=None,
):
    """Use vendor retries under a wall-clock deadline and process-group cancellation."""
    import sys
    from .operations import OperationPlan, OperationRunner, OperationStatus

    argv = [
        sys.executable,
        "-m",
        "bbackup.b2",
        "--bucket",
        bucket,
        "--prefix",
        prefix,
        "--key-id-env",
        key_id_env,
        "--key-env",
        key_env,
    ]
    plan = OperationPlan.create(
        "b2-inspect", "b2-inspect", argv, env=dict(os.environ), timeout_seconds=120
    )
    result = (runner or OperationRunner()).run(plan, cancel_event=cancel)
    if result.status is not OperationStatus.SUCCEEDED or result.exit_code != 0:
        raise B2Error("B2 inspection failed, timed out, or was cancelled")
    try:
        if result.stdout_bytes != len(result.stdout_tail):
            raise ValueError()
        data = strict_json(result.stdout_tail.decode())
        if not isinstance(data, dict) or data.get("provider") != "backblaze-b2":
            raise ValueError()
        return data
    except ValueError:
        raise B2Error("B2 inspection result could not be verified") from None


def main():
    import argparse
    import json

    parser = argparse.ArgumentParser()
    for name in ("bucket", "prefix", "key-id-env", "key-env"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        result = inspect_sdk(
            args.bucket,
            prefix=args.prefix,
            key_id_env=args.key_id_env,
            key_env=args.key_env,
        )
    except B2Error:
        return 3
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
