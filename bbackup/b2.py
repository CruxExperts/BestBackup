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
    "readBucketEncryption", "readBucketRetentions", "readFileRetentions",
    "readFileLegalHolds", "readBucketReplications", "readBucketNotifications",
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
        capabilities = allowed["capabilities"]
        if (
            not isinstance(capabilities, list)
            or any(not isinstance(c, str) for c in capabilities)
            or not {"listBuckets", "listFiles", "readFiles"}.issubset(capabilities)
        ):
            raise B2Error(
                "B2 key needs listBuckets, listFiles, and readFiles for inspection"
            )
        allowed_prefix = allowed.get("namePrefix") or ""
        if not isinstance(allowed_prefix, str) or not prefix.startswith(allowed_prefix):
            raise B2Error("Requested prefix is outside the key scope")
        s3_url = _endpoint(
            api.account_info.get_s3_api_url(), r"s3\.[a-z0-9-]+\.backblazeb2\.com"
        )
        matches = api.list_buckets(bucket_name=bucket)
        if len(matches) != 1 or matches[0].name != bucket:
            raise B2Error("B2 bucket is missing or inaccessible")
        selected = matches[0]
        # Ask for one version to verify listing access without walking a bucket.
        versions = iter(
            selected.ls(path=prefix, latest_only=False, recursive=True, fetch_count=1)
        )
        try:
            next(versions, None)
        finally:
            if hasattr(versions, "close"):
                versions.close()
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
            "version_listing_verified": True,
            "read_capability_present": True,
            "write_capability_present": "writeFiles" in capabilities,
            "credential_administration_denied": not administrative,
            "unsafe_capabilities": administrative,
            "lifecycle_conflicts": sorted(set(conflicts)),
            "observed_noncurrent_days": min(noncurrent_days)
            if noncurrent_days
            else None,
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
