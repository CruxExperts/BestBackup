import shutil
import subprocess

import pytest

from bbackup.recovery import RecoveryError, open_kit, seal_kit, verify_checkpoint


@pytest.fixture
def test_key(tmp_path):
    if not shutil.which("gpg"):
        pytest.skip("requires gpg")
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    base = ["gpg", "--homedir", str(home), "--batch", "--pinentry-mode", "loopback", "--passphrase", ""]
    subprocess.run([*base, "--quick-generate-key", "bbackup disposable test", "ed25519", "sign", "1d"], check=True, capture_output=True)
    keys = subprocess.run([*base, "--with-colons", "--list-secret-keys"], check=True, capture_output=True, text=True).stdout
    fingerprint = next(line.split(":")[9] for line in keys.splitlines() if line.startswith("fpr:"))
    subprocess.run([*base, "--quick-add-key", fingerprint, "cv25519", "encr", "1d"], check=True, capture_output=True)
    yield home, fingerprint
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"], capture_output=True)


def test_sealed_kit_authenticates_and_recovers_independent_password(tmp_path, test_key):
    home, fingerprint = test_key
    manifest = tmp_path / "checkpoint.jsonl"
    manifest.write_text('{"test":"exact version checkpoint"}\n')
    password = tmp_path / "password"
    password.write_text("independent test password")
    password.chmod(0o600)
    kit = tmp_path / "kit.gpg"
    sealed = seal_kit(manifest, password, kit, signer=fingerprint, recipient=fingerprint, home=home)
    destination = tmp_path / "opened"
    opened = open_kit(kit, destination, signer=fingerprint, home=home)
    assert opened["manifest_sha256"] == sealed["manifest_sha256"]
    assert (destination / "repository.password").read_bytes() == password.read_bytes()
    assert (destination / "repository.password").stat().st_mode & 0o777 == 0o600
    assert not sealed["off_host_custody_verified"]
    (destination / "checkpoint.jsonl").write_text("tampered")
    with pytest.raises(RecoveryError):
        verify_checkpoint(destination / "checkpoint.jsonl", destination / "checkpoint.sig", fingerprint, home=home)
    with pytest.raises(RecoveryError):
        open_kit(kit, tmp_path / "untrusted", signer="A" * 40, home=home)


def test_nonregular_and_precancelled_inputs_fail_before_gpg(tmp_path):
    import os
    from threading import Event
    from bbackup.recovery import verify_checkpoint
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(RecoveryError, match="regular"):
        verify_checkpoint(fifo, tmp_path / "sig", "A" * 40)
    cancel = Event()
    cancel.set()
    with pytest.raises(RecoveryError, match="cancelled"):
        verify_checkpoint(tmp_path / "missing", tmp_path / "sig", "A" * 40, cancel=cancel)


def test_validsig_does_not_override_revoked_or_expired_status():
    from types import SimpleNamespace
    from bbackup.recovery import _verify_status
    for status in ("REVKEYSIG", "EXPKEYSIG", "EXPSIG", "BADSIG", "ERRSIG"):
        output = (f"[GNUPG:] {status} key user\n[GNUPG:] VALIDSIG {'A' * 40} 2026-10-03 1 0 4 0 22 8 00 {'A' * 40}\n").encode()
        with pytest.raises(RecoveryError):
            _verify_status(SimpleNamespace(stdout_tail=output, stdout_bytes=len(output)), "A" * 40)


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("restic") is None, reason="requires restic")
def test_historical_repository_and_kit_restore_without_original_ledger(tmp_path, test_key):
    import json
    import io
    from bbackup.cloud import S3CloudAdapter
    from bbackup.models import Configuration, HostBindings, RepositoryBinding
    from bbackup.service import BackupService
    from bbackup.recovery import verify_reconstructed_repository
    from tests.test_cloud import FakeS3

    home, signer = test_key
    data = tmp_path / "data"
    data.mkdir()
    (data / "record.txt").write_text("historical recovery proof")
    password = tmp_path / "password"
    password.write_text("independent escrow password")
    password.chmod(0o600)
    repo = tmp_path / "original-repo"
    config = Configuration.parse({"schema_version": 2, "repositories": [{"name": "local"}],
        "sources": [{"name": "files", "kind": "files", "paths": [str(data)]}],
        "jobs": [{"name": "daily", "repository": "local", "sources": ["files"]}]})
    service = BackupService(config, HostBindings(tmp_path / "state", {"local": RepositoryBinding(str(repo), password)}))
    service.initialize("local")
    captured = service.capture("daily")
    engine = service._engine("local")
    _, result = service._execute("local", [*engine.base_args(), "cat", "config"], label="inspect")
    repository_id = json.loads(result.stdout_tail)["id"]
    versions = {}
    for path in repo.rglob("*"):
        if path.is_file():
            versions["repo/" + str(path.relative_to(repo))] = path.read_bytes()
    page = {"Versions": [{"Key": key, "VersionId": "historical-v1", "IsLatest": True, "Size": len(value)}
                          for key, value in versions.items()], "IsTruncated": False}

    class HistoricalStore(FakeS3):
        def get_object(self, **kwargs):
            assert kwargs["VersionId"] == "historical-v1"
            return {"VersionId": "historical-v1", "Body": io.BytesIO(versions[kwargs["Key"]])}

    adapter = S3CloudAdapter("test-bucket", client=HistoricalStore(pages={(None, None): page}),
                             endpoint_url="https://recovery.example.test", prefix="repo")
    manifest = tmp_path / "checkpoint.jsonl"
    checkpoint = adapter.create_checkpoint(manifest, source_repository="local", repository_id=repository_id,
        source_snapshot_id=captured["snapshot_id"], trusted_quiescent_window_confirmed=True)
    kit = tmp_path / "kit.gpg"
    seal_kit(manifest, password, kit, signer=signer, recipient=signer, home=home)
    # Remove only this test's original data, credentials, repository, ledger and manifest.
    shutil.rmtree(repo)
    shutil.rmtree(tmp_path / "state")
    shutil.rmtree(data)
    password.unlink()
    manifest.unlink()
    opened = tmp_path / "escrow"
    authenticated = open_kit(kit, opened, signer=signer, home=home)
    rebuilt = tmp_path / "rebuilt-repo"
    rebuilt.mkdir(mode=0o700)
    restored = adapter.restore_checkpoint(opened / "checkpoint.jsonl", rebuilt,
        expected_manifest_sha256=authenticated["manifest_sha256"], expected_source_repository="local",
        expected_repository_id=repository_id, expected_source_snapshot_id=captured["snapshot_id"])
    assert restored.files_restored == len(versions)
    verify_reconstructed_repository(rebuilt, opened / "repository.password", repository_id, captured["snapshot_id"])
    clean_config = Configuration.parse({"schema_version": 2, "repositories": [{"name": "recovered"}], "sources": [], "jobs": []})
    clean_service = BackupService(clean_config, HostBindings(tmp_path / "clean-state", {
        "recovered": RepositoryBinding(str(rebuilt), opened / "repository.password")}))
    target = tmp_path / "restored-files"
    clean_service.restore("recovered", captured["snapshot_id"], target)
    assert (target / str(data / "record.txt").lstrip("/")).read_text() == "historical recovery proof"
    assert checkpoint.repository_id == repository_id
