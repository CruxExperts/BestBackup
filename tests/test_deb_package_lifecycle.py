from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import time
import tomllib
import zipfile
from pathlib import Path

import pytest



ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "scripts" / "build_deb.py"
SPEC = importlib.util.spec_from_file_location("bbackup_build_deb", BUILDER_PATH)
assert SPEC is not None and SPEC.loader is not None
BUILDER = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BUILDER
SPEC.loader.exec_module(BUILDER)


def make_assets(tmp_path: Path) -> tuple[dict[str, str], Path, Path, Path, Path]:
    lock_path = tmp_path / "run" / "bbackup-package.lock"
    pending_path = tmp_path / "state" / "upgrade-pending"
    state_dir = pending_path.parent
    version_root = tmp_path / "opt" / "bbackup" / "test-version"
    binary_dir = tmp_path / "bin"
    tmpfiles_file = tmp_path / "tmpfiles" / "bbackup.conf"
    python = tmp_path / "python"
    python.write_text(
        "#!/bin/sh\nprintf launched > \"$LAUNCH_SIGNAL\"\nexec sleep 30\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    replacements = {
        "LOCK_PATH": str(lock_path),
        "PENDING_PATH": str(pending_path),
        "STATE_DIR": str(state_dir),
        "VERSION_ROOT": str(version_root),
        "PYTHON": str(python),
        "BINARY_DIR": str(binary_dir),
        "TMPFILES_FILE": str(tmpfiles_file),
        "PAYLOAD_MANIFEST": str(tmp_path / "lib" / "bbackup" / "payload.sha256"),
        "ROOT_UID": str(os.getuid()),
        "ROOT_GID": str(os.getgid()),
        "ARCH": "amd64",
        "PYTHON_VERSION": "3.12",
    }
    return replacements, lock_path, pending_path, version_root, python


def restore_payload(replacements: dict[str, str], version_root: Path) -> None:
    package_dir = version_root / "site-packages" / "bbackup"
    package_dir.mkdir(parents=True)
    (package_dir / "cli.py").write_text("# restored cli\n", encoding="utf-8")
    (package_dir / "bbman_entry.py").write_text("# restored bbman\n", encoding="utf-8")
    restic = version_root / "bin" / "restic"
    restic.parent.mkdir(parents=True)
    restic.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    restic.chmod(0o755)
    binary_dir = Path(replacements["BINARY_DIR"])
    binary_dir.mkdir(parents=True)
    for name in ("bbackup", "bbman"):
        launcher_asset = "bbackup.in" if name == "bbackup" else "bbman.in"
        launcher = binary_dir / name
        launcher.write_text(BUILDER.render_asset(launcher_asset, replacements), encoding="utf-8")
        launcher.chmod(0o755)
    tmpfiles_file = Path(replacements["TMPFILES_FILE"])
    tmpfiles_file.parent.mkdir(parents=True)
    tmpfiles_file.touch()
    manifest_path = Path(replacements["PAYLOAD_MANIFEST"])
    manifest_path.parent.mkdir(parents=True)
    payload_files = sorted([*version_root.rglob("*"), binary_dir / "bbackup", binary_dir / "bbman"])
    lines = []
    for path in payload_files:
        if not path.is_file():
            continue
        relative = path.relative_to(Path("/")).as_posix()
        lines.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {relative}")
    manifest_path.write_text("\n".join(lines) + "\n", encoding="utf-8")



def render_hook(tmp_path: Path, name: str, replacements: dict[str, str]) -> Path:
    path = tmp_path / f"{name}.sh"
    path.write_text(BUILDER.render_asset(name, replacements), encoding="utf-8")
    path.chmod(0o755)
    return path


def run_hook(path: Path, action: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([str(path), action], capture_output=True, text=True, check=False)


def wait_for(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), "launcher did not enter its long-lived process"



def make_wheel(path: Path, altered_requirement: str | None = None) -> tuple[str, tuple]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    root_package = next(
        package for package in lock["package"]
        if package["name"] == "bbackup" and package.get("source", {}).get("editable") == "."
    )
    version = root_package["version"]
    requirements = [
        requirement["name"]
        + requirement.get("specifier", "")
        + (f"; {requirement['marker']}" if requirement.get("marker") else "")
        for requirement in root_package["metadata"]["requires-dist"]
    ]
    if altered_requirement:
        requirements[0] = altered_requirement
    metadata = (
        "Metadata-Version: 2.1\n"
        "Name: bbackup\n"
        f"Version: {version}\n"
        "Requires-Python: >=3.12\n"
        + "".join(f"Requires-Dist: {requirement}\n" for requirement in requirements)
    )
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("bbackup-" + version.replace("-", "_") + ".dist-info/METADATA", metadata)
        wheel.writestr(
            "bbackup-" + version.replace("-", "_") + ".dist-info/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        wheel.writestr(
            "bbackup-" + version.replace("-", "_") + ".dist-info/entry_points.txt",
            "[console_scripts]\nbbackup = bbackup.cli:cli\nbbman = bbackup.bbman_entry:cli\n",
        )
    return version, BUILDER.read_lock(ROOT / "uv.lock")[2]

def test_prerm_refuses_while_launcher_holds_shared_lock(tmp_path: Path) -> None:
    replacements, lock_path, _, _, _ = make_assets(tmp_path)
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()
    lock_path.chmod(0o644)
    launcher = render_hook(tmp_path, "bbackup.in", replacements)
    prerm = render_hook(tmp_path, "prerm", replacements)
    signal = tmp_path / "launched"
    process = subprocess.Popen(
        [str(launcher)],
        env={**os.environ, "LAUNCH_SIGNAL": str(signal)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for(signal, process)
        refused = run_hook(prerm, "upgrade")
        assert refused.returncode == 75
        assert "is active" in refused.stderr
        assert not Path(replacements["PENDING_PATH"]).exists()
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)


def test_pending_marker_blocks_launcher_until_valid_configure(tmp_path: Path) -> None:
    replacements, lock_path, pending_path, version_root, _ = make_assets(tmp_path)
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()
    lock_path.chmod(0o644)
    preinst = render_hook(tmp_path, "preinst", replacements)
    launcher = render_hook(tmp_path, "bbman.in", replacements)
    postinst = render_hook(tmp_path, "postinst", replacements)

    installed = run_hook(preinst, "install")
    assert installed.returncode == 0, installed.stderr
    assert pending_path.is_file()
    assert pending_path.stat().st_mode & 0o777 == 0o600

    signal = tmp_path / "should-not-launch"
    refused = subprocess.run(
        [str(launcher)],
        env={**os.environ, "LAUNCH_SIGNAL": str(signal)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert refused.returncode == 75
    assert "transition is incomplete" in refused.stderr
    assert not signal.exists()

    restore_payload(replacements, version_root)

    configured = run_hook(postinst, "configure")
    assert configured.returncode == 0, configured.stderr
    assert not pending_path.exists()


@pytest.mark.parametrize("action", ["abort-upgrade", "abort-remove"])
def test_dpkg_abort_clears_marker_only_for_verified_restored_payload(
    tmp_path: Path, action: str
) -> None:
    replacements, lock_path, pending_path, version_root, _ = make_assets(tmp_path)
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()
    lock_path.chmod(0o644)
    pending_path.parent.mkdir(parents=True)
    pending_path.touch()
    pending_path.chmod(0o600)
    restore_payload(replacements, version_root)
    postinst = render_hook(tmp_path, "postinst", replacements)

    recovered = run_hook(postinst, action)
    assert recovered.returncode == 0, recovered.stderr
    assert not pending_path.exists()

    signal = tmp_path / "restored-launcher-ran"
    process = subprocess.Popen(
        [str(Path(replacements["BINARY_DIR"]) / "bbackup")],
        env={**os.environ, "LAUNCH_SIGNAL": str(signal)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for(signal, process)
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)


def test_dpkg_abort_keeps_marker_when_restored_payload_is_incomplete(tmp_path: Path) -> None:
    replacements, lock_path, pending_path, version_root, _ = make_assets(tmp_path)
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()
    lock_path.chmod(0o644)
    pending_path.parent.mkdir(parents=True)
    pending_path.touch()
    pending_path.chmod(0o600)
    restore_payload(replacements, version_root)
    (version_root / "site-packages" / "bbackup" / "cli.py").unlink()
    postinst = render_hook(tmp_path, "postinst", replacements)

    failed_recovery = run_hook(postinst, "abort-upgrade")
    assert failed_recovery.returncode != 0
    assert pending_path.exists()

    launcher = Path(replacements["BINARY_DIR"]) / "bbackup"
    refused = subprocess.run([str(launcher)], capture_output=True, text=True, check=False)
    assert refused.returncode == 75
    assert "transition is incomplete" in refused.stderr


def test_dpkg_abort_without_marker_allows_active_client_with_verified_payload(
    tmp_path: Path,
) -> None:
    replacements, lock_path, pending_path, version_root, _ = make_assets(tmp_path)
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()
    lock_path.chmod(0o644)
    restore_payload(replacements, version_root)
    postinst = render_hook(tmp_path, "postinst", replacements)
    signal = tmp_path / "active-client"
    launcher = Path(replacements["BINARY_DIR"]) / "bbackup"
    process = subprocess.Popen(
        [str(launcher)],
        env={**os.environ, "LAUNCH_SIGNAL": str(signal)},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for(signal, process)
        recovered = run_hook(postinst, "abort-upgrade")
        assert recovered.returncode == 0, recovered.stderr
        assert not pending_path.exists()
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)


def test_dpkg_abort_without_marker_refuses_damaged_payload(tmp_path: Path) -> None:
    replacements, lock_path, pending_path, version_root, _ = make_assets(tmp_path)
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()
    lock_path.chmod(0o644)
    restore_payload(replacements, version_root)
    (version_root / "site-packages" / "bbackup" / "cli.py").write_text(
        "# damaged restored payload\n", encoding="utf-8"
    )
    postinst = render_hook(tmp_path, "postinst", replacements)

    refused = run_hook(postinst, "abort-upgrade")

    assert refused.returncode != 0
    assert "checksum verification failed" in refused.stderr
    assert not pending_path.exists()


def test_dpkg_abort_without_marker_refuses_exclusive_transition_writer(
    tmp_path: Path,
) -> None:
    replacements, lock_path, pending_path, version_root, _ = make_assets(tmp_path)
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()
    lock_path.chmod(0o644)
    restore_payload(replacements, version_root)
    postinst = render_hook(tmp_path, "postinst", replacements)
    holder_signal = tmp_path / "exclusive-lock-held"
    holder_script = tmp_path / "hold-exclusive-lock.sh"
    holder_script.write_text(
        "#!/bin/sh\n"
        "exec 9<>\"$1\"\n"
        "flock --exclusive --nonblock 9 || exit 1\n"
        ": > \"$2\"\n"
        "exec sleep 30\n",
        encoding="utf-8",
    )
    holder_script.chmod(0o755)
    holder = subprocess.Popen(
        [str(holder_script), str(lock_path), str(holder_signal)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for(holder_signal, holder)
        refused = run_hook(postinst, "abort-upgrade")
        assert refused.returncode == 75
        assert "package transition is active" in refused.stderr
        assert not pending_path.exists()
    finally:
        if holder.poll() is None:
            holder.terminate()
        holder.communicate(timeout=5)


@pytest.mark.parametrize(
    ("soabi", "machine", "expected"),
    [
        ("cpython-312-x86_64-linux-gnu", "x86_64", True),
        ("cpython-311-x86_64-linux-gnu", "x86_64", False),
        ("cpython-312-aarch64-linux-gnu", "x86_64", False),
        ("cp312-x86_64-linux-gnu", "x86_64", False),
    ],
)
def test_native_cpython_abi_matches_version_and_architecture(
    soabi: str, machine: str, expected: bool
) -> None:
    assert BUILDER.has_native_cpython_abi(soabi, 3, 12, machine) is expected



def test_builder_derives_systemd_templates_from_installed_package_code() -> None:
    templates = BUILDER.render_systemd_templates(ROOT)

    assert set(templates) == {"bbackup@.service", "bbackup@.timer"}
    assert "OnCalendar=daily" in templates["bbackup@.timer"]
    assert "Unit=bbackup@%i.service" in templates["bbackup@.timer"]
    assert "/etc/bbackup/config.json" in templates["bbackup@.service"]



def test_deb_artifact_stems_distinguish_all_release_targets() -> None:
    stems = {
        BUILDER.deb_artifact_stem("2.0.0~a1", release, architecture)
        for release in ("24.04", "26.04")
        for architecture in ("amd64", "arm64")
    }
    assert stems == {
        "bbackup_2.0.0~a1_ubuntu24.04_amd64",
        "bbackup_2.0.0~a1_ubuntu24.04_arm64",
        "bbackup_2.0.0~a1_ubuntu26.04_amd64",
        "bbackup_2.0.0~a1_ubuntu26.04_arm64",
    }


def test_wheel_requires_dist_must_match_current_lock_metadata(tmp_path: Path) -> None:
    version = BUILDER.read_lock(ROOT / "uv.lock")[1]
    valid_wheel = tmp_path / f"bbackup-{version}-py3-none-any.whl"
    version, expected = make_wheel(valid_wheel)
    BUILDER.inspect_wheel(valid_wheel, version, "3.12", "amd64", expected)

    incompatible_wheel = tmp_path / f"bbackup-{version}-1-py3-none-any.whl"
    version, expected = make_wheel(incompatible_wheel, "b2sdk==0.0")
    with pytest.raises(BUILDER.BuildError, match="Requires-Dist"):
        BUILDER.inspect_wheel(incompatible_wheel, version, "3.12", "amd64", expected)
