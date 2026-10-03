"""Independent, signed and GnuPG-encrypted recovery kit artifacts."""
from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path
import re
import tarfile
import tempfile

from .operations import OperationPlan, OperationRunner, OperationStatus


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
            valid.append(fields[2].upper() == signer or fields[-1].upper() == signer)
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
