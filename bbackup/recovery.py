"""Independent, signed and GnuPG-encrypted recovery kit artifacts."""
from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import re
import tarfile
import tempfile

from .operations import OperationPlan, OperationRunner, OperationStatus, repository_key
from .cloud import _COPYRIGHT_URL, _PRIVATE_NOTICE


class RecoveryError(RuntimeError):
    """Recovery artifact validation failed without exposing credentials."""


_MAX_ARTIFACT = 34 * 1024**3


def _cancelled(cancel):
    if cancel is not None and cancel.is_set():
        raise RecoveryError("Recovery operation cancelled")


@contextmanager
def _regular(path, limit):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise RecoveryError("Recovery input must be a bounded regular file")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            yield stream
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _copy(source, target, *, limit=_MAX_ARTIFACT, cancel=None):
    _cancelled(cancel)
    with _regular(source, limit) as incoming, target.open("xb") as output:
        target.chmod(0o600)
        total = 0
        while True:
            _cancelled(cancel)
            chunk = incoming.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise RecoveryError("Recovery input exceeded its size limit")
            output.write(chunk)


class _CancellableReader:
    def __init__(self, stream, cancel):
        self.stream, self.cancel = stream, cancel

    def read(self, size=-1):
        _cancelled(self.cancel)
        return self.stream.read(size)


def _verify_status(result, signer):
    if result.stdout_bytes != len(result.stdout_tail):
        raise RecoveryError("GnuPG signature status was truncated")
    valid = []
    denied = {"REVKEYSIG", "EXPKEYSIG", "EXPSIG", "BADSIG", "ERRSIG", "NO_PUBKEY", "NODATA", "FAILURE"}
    for line in result.stdout_tail.decode("ascii", errors="replace").splitlines():
        fields = line.split()
        if len(fields) < 2 or fields[0] != "[GNUPG:]":
            continue
        if fields[1] in denied:
            raise RecoveryError("Signature is revoked, expired, invalid or unverifiable")
        if fields[1] == "VALIDSIG":
            if len(fields) < 11:
                raise RecoveryError("Malformed signature status")
            # The configured identity is the primary key. Accepting a signing
            # subkey fingerprint here would let source and verifier be two
            # fingerprints from the same OpenPGP certificate.
            valid.append(fields[-1].upper() == signer)
    if valid != [True]:
        raise RecoveryError("Signature does not match the trusted signer")


def _fingerprint(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64}", value):
        raise RecoveryError("An explicit complete OpenPGP fingerprint is required")
    return value.upper()


def _gpg(args, *, home=None, cancel=None):
    argv = ["gpg", "--batch", "--no-tty", "--pinentry-mode", "error"]
    if home is not None:
        argv += ["--homedir", str(home)]
    result = OperationRunner().run(OperationPlan.create("recovery-kit", "gpg", [*argv, *args], timeout_seconds=120), cancel_event=cancel)
    if result.status is not OperationStatus.SUCCEEDED:
        raise RecoveryError("GnuPG operation failed")
    return result


def verify_checkpoint(manifest: Path, signature: Path, signer: str, *, home=None, cancel=None) -> str:
    signer = _fingerprint(signer)
    with tempfile.TemporaryDirectory(prefix="bbackup-verify-") as temporary:
        root = Path(temporary)
        pinned = root / "checkpoint"
        pinned_signature = root / "signature"
        for source, target in ((manifest, pinned), (signature, pinned_signature)):
            _copy(source, target, limit=1024**2 if source == signature else _MAX_ARTIFACT, cancel=cancel)
        result = _gpg(["--status-fd", "1", "--verify", "--", str(pinned_signature), str(pinned)], home=home, cancel=cancel)
        _verify_status(result, signer)
        digest = hashlib.sha256()
        with pinned.open("rb") as stream:
            while True:
                _cancelled(cancel)
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()



def seal_kit(manifest: Path, password_file: Path, destination: Path, *, signer: str, recipient: str, home=None, cancel=None):
    """Seal a checkpoint and independent restic password for off-host custody.

    This creates an artifact; it cannot establish that an administrator actually
    moved it off the protected host or verified a recovery drill.
    """
    signer, recipient = _fingerprint(signer), _fingerprint(recipient)
    if password_file.stat().st_mode & 0o077:
        raise RecoveryError("Repository password file must be private")
    if not destination.parent.is_dir():
        raise RecoveryError("Recovery kit parent must exist")
    with tempfile.TemporaryDirectory(prefix="bbackup-kit-") as temporary:
        root = Path(temporary)
        pinned = root / "checkpoint.jsonl"
        _copy(manifest, pinned, cancel=cancel)
        pinned_password = root / "repository.password"
        _copy(password_file, pinned_password, limit=65536, cancel=cancel)
        password_file = pinned_password
        manifest = pinned
        signature = root / "checkpoint.sig"
        _gpg(["--local-user", signer, "--output", str(signature), "--detach-sign", str(manifest)], home=home, cancel=cancel)
        digest = verify_checkpoint(manifest, signature, signer, home=home, cancel=cancel)
        archive = root / "kit.tar"
        with tarfile.open(archive, "w") as bundle:
            for path, name in ((manifest, "checkpoint.jsonl"), (signature, "checkpoint.sig"), (password_file, "repository.password")):
                info = bundle.gettarinfo(str(path), arcname=name)
                if not info.isfile():
                    raise RecoveryError("Recovery kit inputs must be regular files")
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o600
                with path.open("rb") as stream:
                    bundle.addfile(info, _CancellableReader(stream, cancel))
        encrypted = root / "kit.gpg"
        _gpg(["--local-user", signer, "--recipient", recipient, "--output", str(encrypted), "--sign", "--encrypt", str(archive)], home=home, cancel=cancel)
        # Exclusive create prevents replacement of a previous independent kit.
        _copy(encrypted, destination, cancel=cancel)
    return {"manifest_sha256": digest, "signer": signer, "recipient": recipient,
            "off_host_custody_verified": False, "recovery_demonstrated": False}


def open_kit(kit: Path, destination: Path, *, signer: str, home=None, cancel=None):
    """Decrypt and authenticate into a new directory, rejecting unexpected members."""
    signer = _fingerprint(signer)
    if destination.exists() or any(p.is_symlink() for p in (destination, *destination.parents)):
        raise RecoveryError("Recovery kit destination must be a new directory without symlinks")
    with tempfile.TemporaryDirectory(prefix="bbackup-open-kit-") as temporary:
        root = Path(temporary)
        archive = root / "kit.tar"
        pinned_kit = root / "kit.gpg"
        _copy(kit, pinned_kit, cancel=cancel)
        result = _gpg(["--max-output", str(_MAX_ARTIFACT), "--status-fd", "1", "--output", str(archive), "--decrypt", "--", str(pinned_kit)], home=home, cancel=cancel)
        _verify_status(result, signer)
        allowed = {"checkpoint.jsonl": 32 * 1024**3, "checkpoint.sig": 1024**2, "repository.password": 65536}
        seen = set()
        with tarfile.open(archive, "r|") as bundle:
            for member in bundle:
                _cancelled(cancel)
                if member.name not in allowed or member.name in seen or not member.isfile() or member.size > allowed[member.name]:
                    raise RecoveryError("Invalid recovery kit archive member")
                seen.add(member.name)
                stream = bundle.extractfile(member)
                if stream is None:
                    raise RecoveryError("Unreadable recovery kit member")
                with stream, (root / member.name).open("xb") as output:
                    (root / member.name).chmod(0o600)
                    while chunk := stream.read(1024 * 1024):
                        _cancelled(cancel)
                        output.write(chunk)
        if seen != set(allowed):
            raise RecoveryError("Recovery kit is incomplete")
        digest = verify_checkpoint(root / "checkpoint.jsonl", root / "checkpoint.sig", signer, home=home, cancel=cancel)
        destination.mkdir(mode=0o700, parents=False, exist_ok=False)
        for name in allowed:
            # Copy rather than rename across filesystems; destination stays private.
            _copy(root / name, destination / name, limit=allowed[name], cancel=cancel)
        return {"manifest_sha256": digest, "signer": signer, "recovery_demonstrated": False}


def create_checked_kit(service, adapter, repository, snapshot, destination: Path, *, signer, recipient, quiescent_window_confirmed, cancel=None):
    """Verify a bound repository and capture/seal its exact versions under the local lease."""
    import json
    from dataclasses import asdict
    from .service import _snapshot_id
    if not quiescent_window_confirmed:
        raise RecoveryError("An administrator must coordinate a quiescent window across all writers")
    _snapshot_id(snapshot)
    binding = service.bindings.repositories.get(repository)
    if binding is None:
        raise RecoveryError("Unknown repository")
    endpoint = adapter.endpoint_url
    if not endpoint:
        raise RecoveryError("Explicit S3 endpoint required to bind the checkpoint to the repository")
    expected = "s3:" + endpoint.rstrip("/") + "/" + adapter.bucket
    if adapter.prefix:
        expected += "/" + adapter.prefix.rstrip("/")
    if binding.repository.rstrip("/") != expected:
        raise RecoveryError("Checkpoint object prefix does not match the checked restic repository")
    engine = service._engine(repository)
    result = {}
    def finalize(plan, _run):
        values = {}
        for kind, identifier in (("config", None), ("snapshot", snapshot)):
            argv = [*engine.base_args(), "cat", kind]
            if identifier:
                argv.append(identifier)
            inspection = service.runner.run(OperationPlan.create(service._identity(repository), "inspect", argv, env=plan.env), cancel_event=cancel)
            if inspection.status is not OperationStatus.SUCCEEDED:
                raise RecoveryError("Repository identity or snapshot could not be verified")
            try:
                values[kind] = json.loads(inspection.stdout_tail)
            except (ValueError, TypeError):
                raise RecoveryError("Invalid repository identity response") from None
        repository_id = values["config"].get("id")
        with tempfile.TemporaryDirectory(prefix="checkpoint-", dir=service.state) as temporary:
            manifest = Path(temporary) / "checkpoint.jsonl"
            checkpoint = adapter.create_checkpoint(manifest, source_repository=repository,
                repository_id=repository_id, source_snapshot_id=snapshot, trusted_quiescent_window_confirmed=True, cancel_event=cancel)
            result.update(asdict(checkpoint))
            result.pop("path", None)
            result.update(seal_kit(manifest, binding.password_file, destination, signer=signer, recipient=recipient, cancel=cancel))
            with service._receipts() as db:
                db.execute(
                    """INSERT INTO checkpoints
                       (repository, repository_identity, snapshot, operation,
                        manifest_sha256, captured_at, expires_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (repository, repository_key(service._identity(repository)),
                     snapshot, plan.operation_id, result["manifest_sha256"],
                     result["captured_at"], result["expires_at"]),
                )
    plan, _ = service._execute(repository, [*engine.check_args(), "--read-data"], label="checkpoint", finalize=finalize, cancel=cancel)
    result["operation_id"] = plan.operation_id
    return result


def verify_reconstructed_repository(repository: Path, password_file: Path, repository_id: str, snapshot: str, *, cancel=None):
    """Validate the reconstructed encrypted repository independently of a local ledger."""
    import json
    from .config import SnapshotProfile
    from .snapshot import ResticRunner
    if password_file.stat().st_mode & 0o077:
        raise RecoveryError("Repository password file must be private")
    with _regular(password_file, 65536):
        pass
    engine = ResticRunner(SnapshotProfile(name="recovered", repository=str(repository.resolve()),
                                         password_file=str(password_file.resolve()), retry_lock="0s"))
    env = {key: value for key, value in os.environ.items() if not key.startswith("RESTIC_")}
    for args, expected_id in (([*engine.base_args(), "cat", "config"], repository_id),
                              ([*engine.check_args(), "--read-data"], None),
                              ([*engine.base_args(), "cat", "snapshot", snapshot], None)):
        result = OperationRunner().run(OperationPlan.create(repository, "verify-recovery", args, env=env), cancel_event=cancel)
        if result.status is not OperationStatus.SUCCEEDED:
            raise RecoveryError("Reconstructed repository verification failed")
        if expected_id is not None:
            try:
                if json.loads(result.stdout_tail).get("id") != expected_id:
                    raise RecoveryError("Reconstructed repository identity differs from checkpoint")
            except (ValueError, AttributeError):
                raise RecoveryError("Invalid reconstructed repository identity") from None


_ACCEPTANCE_FIELDS = {
    "type", "schema_version", "manifest_sha256", "source_signer",
    "verifier", "source_repository", "repository_id", "snapshot_id",
    "captured_at", "expires_at", "verified_at", "files_restored",
    "bytes_restored", "recovery_verified", "protection_qualified",
    "copyright", "copyright_url", "private_notice",
}
_ID = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def _acceptance_time(value):
    if not isinstance(value, str) or len(value) > 40:
        raise RecoveryError("Invalid recovery acceptance timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RecoveryError("Invalid recovery acceptance timestamp") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RecoveryError("Invalid recovery acceptance timestamp")
    return parsed.astimezone(timezone.utc)


def _acceptance_identity(source_repository, repository_id, snapshot):
    if not isinstance(source_repository, str) or not _NAME.fullmatch(source_repository):
        raise RecoveryError("Invalid source repository identity")
    if not isinstance(repository_id, str) or not _ID.fullmatch(repository_id):
        raise RecoveryError("Invalid repository identity")
    if not isinstance(snapshot, str) or not _ID.fullmatch(snapshot):
        raise RecoveryError("A complete snapshot identity is required")


def _acceptance_record(raw, *, verifier, repository_id, snapshot):
    try:
        record = json.loads(raw, object_pairs_hook=lambda pairs: _unique_pairs(pairs))
    except (ValueError, TypeError, UnicodeDecodeError):
        raise RecoveryError("Invalid recovery acceptance record") from None
    if not isinstance(record, dict) or set(record) != _ACCEPTANCE_FIELDS:
        raise RecoveryError("Invalid recovery acceptance schema")
    if record["type"] != "recovery_acceptance" or type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise RecoveryError("Unsupported recovery acceptance schema")
    _acceptance_identity(record["source_repository"], record["repository_id"], record["snapshot_id"])
    if record["repository_id"] != repository_id or record["snapshot_id"] != snapshot:
        raise RecoveryError("Recovery acceptance identity differs from expected repository")
    if not isinstance(record["manifest_sha256"], str) or not _ID.fullmatch(record["manifest_sha256"]):
        raise RecoveryError("Invalid recovery acceptance manifest digest")
    if _fingerprint(record["verifier"]) != verifier or _fingerprint(record["source_signer"]) == verifier:
        raise RecoveryError("Recovery acceptance signer trust differs from central configuration")
    if record["recovery_verified"] is not True or record["protection_qualified"] is not False:
        raise RecoveryError("Recovery acceptance makes an unsupported claim")
    if any(type(record[key]) is not int or record[key] < 0 for key in ("files_restored", "bytes_restored")):
        raise RecoveryError("Invalid recovery acceptance restore counts")
    captured = _acceptance_time(record["captured_at"])
    verified = _acceptance_time(record["verified_at"])
    expiry = record["expires_at"]
    if captured > verified or (expiry is not None and (_acceptance_time(expiry) <= verified or _acceptance_time(expiry) <= captured)):
        raise RecoveryError("Recovery acceptance time is outside the checkpoint window")
    if (record["copyright"] != f"Copyright © {verified.year} Crux Experts LLC"
            or record["copyright_url"] != _COPYRIGHT_URL
            or record["private_notice"] != _PRIVATE_NOTICE):
        raise RecoveryError("Recovery acceptance ownership metadata is invalid")
    return record


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _publish_acceptance_noreplace(staging: Path, destination: Path):
    """Atomically publish a complete directory without replacing even an empty one."""
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise RecoveryError("Atomic exclusive acceptance publication is unavailable")
    renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    renameat2.restype = ctypes.c_int
    if renameat2(-100, os.fsencode(staging), -100, os.fsencode(destination), 1) != 0:
        if ctypes.get_errno() == errno.EEXIST:
            raise RecoveryError("Recovery acceptance destination already exists")
        raise RecoveryError("Atomic exclusive acceptance publication failed")
    parent_fd = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def verify_recovery_acceptance(record_dir: Path, *, trusted_verifier: str, repository_id: str,
                               snapshot: str, home=None, cancel=None):
    """Authenticate a central acceptance against an independently configured verifier key."""
    verifier = _fingerprint(trusted_verifier)
    _acceptance_identity("recovered", repository_id, snapshot)
    with tempfile.TemporaryDirectory(prefix="bbackup-acceptance-verify-") as temporary:
        pinned = Path(temporary) / "acceptance.json"
        signature = Path(temporary) / "acceptance.sig"
        _copy(Path(record_dir) / "acceptance.json", pinned, limit=16384, cancel=cancel)
        _copy(Path(record_dir) / "acceptance.sig", signature, limit=1024**2, cancel=cancel)
        result = _gpg(["--status-fd", "1", "--verify", "--", str(signature), str(pinned)], home=home, cancel=cancel)
        _verify_status(result, verifier)
        return _acceptance_record(pinned.read_bytes(), verifier=verifier, repository_id=repository_id, snapshot=snapshot)


def accept_recovery_checkpoint(adapter, kit: Path, record_dir: Path, *, source_signer: str,
                               trusted_verifier: str, source_repository: str, repository_id: str,
                               snapshot: str, home=None, cancel=None):
    """Centrally sign recovery proof after rebuilding and fully checking exact versions.

    The verifier signing key must be held away from the source host. The caller
    supplies trusted source and verifier fingerprints from central configuration;
    possession of a valid but unconfigured key is insufficient.
    """
    source_signer, verifier = _fingerprint(source_signer), _fingerprint(trusted_verifier)
    if source_signer == verifier:
        raise RecoveryError("Source and central verifier keys must differ")
    _acceptance_identity(source_repository, repository_id, snapshot)
    record_dir = Path(record_dir)
    if record_dir.exists() or record_dir.is_symlink() or not record_dir.parent.is_dir() or any(
        parent.is_symlink() for parent in record_dir.parents
    ):
        raise RecoveryError("Recovery acceptance destination must be a new local directory")
    with tempfile.TemporaryDirectory(prefix="bbackup-central-", dir=record_dir.parent) as temporary:
        root = Path(temporary)
        opened = root / "opened"
        authenticated = open_kit(Path(kit), opened, signer=source_signer, home=home, cancel=cancel)
        restored = root / "restored"
        restored.mkdir(mode=0o700)
        result = adapter.restore_checkpoint(opened / "checkpoint.jsonl", restored,
            expected_manifest_sha256=authenticated["manifest_sha256"],
            expected_source_repository=source_repository, expected_repository_id=repository_id,
            expected_source_snapshot_id=snapshot, cancel_event=cancel)
        if result.manifest_sha256 != authenticated["manifest_sha256"]:
            raise RecoveryError("Reconstructed manifest digest differs from authenticated kit")
        verify_reconstructed_repository(restored, opened / "repository.password", repository_id, snapshot, cancel=cancel)
        with _regular(opened / "checkpoint.jsonl", _MAX_ARTIFACT) as stream:
            header = stream.readline(1024 * 1024 + 1)
        try:
            from .cloud import _strict_json
            source = _strict_json(header)
        except (ValueError, TypeError):
            raise RecoveryError("Invalid authenticated checkpoint header") from None
        if not isinstance(source, dict) or source.get("type") != "checkpoint":
            raise RecoveryError("Invalid authenticated checkpoint header")
        verified_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        captured_at, expires_at = source.get("captured_at"), source.get("expires_at")
        if _acceptance_time(captured_at) > _acceptance_time(verified_at) or (
            expires_at is not None and _acceptance_time(expires_at) <= _acceptance_time(verified_at)
        ):
            raise RecoveryError("Checkpoint is future-dated or expired at central verification")
        record = {
            "type": "recovery_acceptance", "schema_version": 1,
            "manifest_sha256": authenticated["manifest_sha256"],
            "source_signer": source_signer, "verifier": verifier,
            "source_repository": source_repository, "repository_id": repository_id,
            "snapshot_id": snapshot, "captured_at": captured_at,
            "expires_at": expires_at, "verified_at": verified_at,
            "files_restored": result.files_restored, "bytes_restored": result.bytes_restored,
            "recovery_verified": True, "protection_qualified": False,
            "copyright": f"Copyright © {_acceptance_time(verified_at).year} Crux Experts LLC",
            "copyright_url": _COPYRIGHT_URL, "private_notice": _PRIVATE_NOTICE,
        }
        payload = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        if len(payload) > 16384:
            raise RecoveryError("Recovery acceptance record is too large")
        _acceptance_record(payload, verifier=verifier, repository_id=repository_id, snapshot=snapshot)
        unsigned = root / "acceptance.json"
        with unsigned.open("xb") as stream:
            unsigned.chmod(0o600)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        signature = root / "acceptance.sig"
        _gpg(["--local-user", verifier, "--output", str(signature), "--detach-sign", str(unsigned)], home=home, cancel=cancel)
        verify_recovery_acceptance(root, trusted_verifier=verifier, repository_id=repository_id, snapshot=snapshot, home=home, cancel=cancel)
        # The staged pair is complete and durable before its single exclusive publish.
        staging = root / "publish"
        staging.mkdir(mode=0o700)
        for name in ("acceptance.json", "acceptance.sig"):
            _copy(root / name, staging / name, limit=16384 if name.endswith(".json") else 1024**2, cancel=cancel)
            with (staging / name).open("rb") as stream:
                os.fsync(stream.fileno())
        verify_recovery_acceptance(staging, trusted_verifier=verifier,
            repository_id=repository_id, snapshot=snapshot, home=home, cancel=cancel)
        directory_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        _publish_acceptance_noreplace(staging, record_dir)
    return record
