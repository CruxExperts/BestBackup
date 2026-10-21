"""Exercise guest evidence predicates without starting a VM or host service."""
import ast
import json
import os
import shlex
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.qualify_systemd import COVERED_ROOT_GUEST_STAGE, GUEST_STAGE


def _covered_function(name, namespace):
    tree = ast.parse(COVERED_ROOT_GUEST_STAGE)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == name)
    exec(compile(ast.Module(body=[function], type_ignores=[]),
                 "<covered-root guest function>", "exec"), namespace)
    return namespace[name]


def _covered_literal(name):
    tree = ast.parse(COVERED_ROOT_GUEST_STAGE)
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == name
                              for target in node.targets))
    return ast.literal_eval(assignment.value)


@pytest.mark.parametrize("command_available", [False, True])
def test_covered_root_acl_setter_reports_unavailable_or_refused_acl(tmp_path, command_available):
    probe = tmp_path / "probe"
    probe.write_bytes(b"fixture")
    acl_uid = max(1000, probe.stat().st_uid + 1)
    namespace = {
        "pwd": SimpleNamespace(getpwall=lambda: [SimpleNamespace(pw_uid=acl_uid)]),
        "os": os,
        "shutil": SimpleNamespace(which=lambda _: "/usr/bin/setfacl" if command_available else None),
        "subprocess": SimpleNamespace(PIPE=-1, run=lambda *args, **kwargs:
                                      SimpleNamespace(returncode=1)),
        "ctypes": SimpleNamespace(util=SimpleNamespace(find_library=lambda _: None)),
    }
    assert _covered_function("set_acl_if_available", namespace)(probe) is None


@pytest.mark.parametrize("raw, expected", [
    (struct.pack("<IHHI", 2, 2, 4, 2000), True),
    (struct.pack("<IHHI", 2, 2, 4, 2001), False),
    (struct.pack("<IHHI", 2, 2, 2, 2000), False),
    (struct.pack("<IHHI", 2, 2, 4, 2000)[:-1], False),
])
def test_covered_root_acl_readback_requires_named_user_read_entry(raw, expected):
    namespace = {"os": SimpleNamespace(getxattr=lambda *args: raw)}
    assert _covered_function("has_fixture_acl", namespace)("probe", 2000) is expected


def test_covered_root_acl_readback_rejects_missing_xattr():
    def missing(*args):
        raise OSError("ACL xattr unavailable")

    namespace = {"os": SimpleNamespace(getxattr=missing)}
    assert _covered_function("has_fixture_acl", namespace)("probe", 2000) is False


@pytest.mark.parametrize("acl_uid, readback, message", [
    (None, False, "cannot create the required fixture ACL"),
    (2000, False, "did not survive readback"),
])
def test_covered_root_source_fixture_fails_without_verified_acl(
        tmp_path, acl_uid, readback, message):
    from scripts import qualify_systemd as module

    namespace = vars(module).copy()
    namespace.update({
        "DATA_ROOT": tmp_path,
        "FILES": tmp_path / "files",
        "os": SimpleNamespace(link=os.link, setxattr=lambda *args: None,
                              chown=lambda *args: None),
        "shutil": SimpleNamespace(rmtree=shutil.rmtree, chown=lambda *args, **kwargs: None),
        "set_acl_if_available": lambda path: acl_uid,
        "has_fixture_acl": lambda path, uid: readback,
    })
    create_app_files = _covered_function("create_app_files", namespace)
    with pytest.raises(RuntimeError, match=message):
        create_app_files(52000, 52000)


def _predicate(name, output):
    tree = ast.parse(GUEST_STAGE)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    calls = []
    def userctl(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(stdout=output)
    namespace = {"userctl": userctl}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "<guest predicate>", "exec"), namespace)
    return namespace[name]("daily"), calls


def test_guest_stage_is_valid_python_before_any_guest_boot():
    compile(GUEST_STAGE, "<qualification guest>", "exec")


def test_covered_root_guest_stage_is_valid_python_before_any_guest_boot():
    compile(COVERED_ROOT_GUEST_STAGE, "<covered-root qualification guest>", "exec")


@pytest.mark.parametrize("include_fixture", [False, True])
def test_covered_root_unit_verification_waits_for_restored_fixture(include_fixture):
    calls = []
    namespace = {"Path": Path, "JOB_NAME": "covered", "APP_UNIT": "fixture.service",
                 "run": lambda argv: calls.append(argv)}
    _covered_function("verify_installed_units", namespace)(include_fixture=include_fixture)
    assert calls == [["/usr/bin/systemd-analyze", "verify",
                      "/etc/systemd/system/bbackup-covered.service",
                      "/etc/systemd/system/bbackup-covered.timer",
                      *(["/etc/systemd/system/fixture.service"] if include_fixture else [])]]


def test_covered_root_replacement_verifies_fixture_after_reconstruction(tmp_path):
    material = tmp_path / "material"
    recipes = material / "recipes"
    recipes.mkdir(parents=True)
    (material / "policy-candidate.json").write_text("{}", encoding="utf-8")
    (material / "bindings-candidate.json").write_text("{}", encoding="utf-8")
    events = []

    class FinishedVerification(Exception):
        pass

    def production(*args, **kwargs):
        events.append(("production", args[0]))
        return None, {"data": {"files_verified": True}}

    def verify_installed_units(*, include_fixture):
        events.append(("verify", include_fixture))
        raise FinishedVerification

    namespace = {"mount_inputs": lambda: None,
                 "mount_recovery": lambda role: None,
                 "install_candidate": lambda candidate: None,
                 "read_json": lambda path: {"source_machine_digest": "source",
                                            "snapshot_id": "snapshot"},
                 "machine_digest": lambda: "replacement",
                 "CONFIG_DIR": tmp_path / "config", "CONFIG": tmp_path / "config" / "config.json",
                 "BINDINGS": tmp_path / "config" / "bindings.json",
                 "MATERIAL": material, "RESTORE_TARGET": tmp_path / "restore",
                 "shutil": shutil, "production": production,
                 "verify_root_enrollment": lambda: None,
                 "install_production_units": lambda **kwargs: events.append(("install", kwargs)),
                 "run": lambda argv, **kwargs: (events.append(("run", Path(argv[1]).name))
                                                or SimpleNamespace(returncode=0)),
                 "verify_installed_units": verify_installed_units}
    replacement_role = _covered_function("replacement_role", namespace)
    with pytest.raises(FinishedVerification):
        replacement_role("candidate.whl")
    assert events == [("production", "service"), ("install", {"include_fixture": False}),
                      ("run", "base-reconstruction.py"), ("production", "restore"),
                      ("run", "fixture-reconstruction.py"), ("verify", True)]


def test_covered_root_guest_records_command_failure_before_raising(tmp_path):
    log_path = tmp_path / "guest.log"
    namespace = {
        "LOG": log_path,
        "os": os,
        "subprocess": SimpleNamespace(PIPE=-1, STDOUT=-2,
            run=lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="offline wheel rejected")),
    }
    namespace["log"] = _covered_function("log", namespace)
    guest_run = _covered_function("run", namespace)
    with pytest.raises(RuntimeError, match=r"command failed \(2\)"):
        guest_run(["uv", "pip", "install"])
    recorded = log_path.read_text(encoding="utf-8")
    assert "COMMAND start: uv pip install" in recorded
    assert "COMMAND rc=2: uv pip install" in recorded
    assert "offline wheel rejected" in recorded


def test_covered_root_restore_diagnostic_preserves_cli_envelope(tmp_path, capsys):
    from bbackup import production_cli

    diagnostic = tmp_path / "diagnostics"
    diagnostic.mkdir(mode=0o700)
    shim = _covered_literal("RESTORE_DIAGNOSTIC_SITE").replace(
        "/var/log/bbackup-covered-root-restore-diagnostics", str(diagnostic))
    (diagnostic / "sitecustomize.py").write_text(shim, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(diagnostic) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    startup = subprocess.run([sys.executable, "-c", "import bbackup.cli; print('ready')"],
                             env=env, capture_output=True, text=True, check=False)
    assert startup.returncode == 0 and startup.stdout.strip() == "ready"
    assert not startup.stderr
    original_emit = production_cli._emit
    try:
        exec(compile(shim, "<restore diagnostic site>", "exec"), {})
        try:
            raise ValueError("synthetic restore failure")
        except ValueError:
            production_cli._emit("restore", error={"code": "operation_failed",
                                                   "message": "sanitized"})
    finally:
        production_cli._emit = original_emit
    assert json.loads(capsys.readouterr().out)["errors"][0]["message"] == "sanitized"
    trace = (diagnostic / "exception.txt").read_text(encoding="utf-8")
    assert "ValueError: synthetic restore failure" in trace
    assert (diagnostic / "exception.txt").stat().st_mode & 0o777 == 0o600


def test_covered_root_restore_dispatch_keeps_cli_arguments_and_private_hook(tmp_path):
    calls = []

    def launch(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"success": true, "data": {}}\n')

    namespace = {"BIN": Path("/opt/bbackup/venv/bin/bbackup"),
                 "CONFIG": Path("/etc/bbackup/covered-root/config.v3.json"),
                 "BINDINGS": Path("/etc/bbackup/covered-root/bindings.v3.json"),
                 "RESTORE_DIAGNOSTICS": tmp_path / "diagnostics",
                 "RESTORE_DIAGNOSTIC_SITE": _covered_literal("RESTORE_DIAGNOSTIC_SITE"),
                 "os": os, "json": json, "log": lambda _: None,
                 "subprocess": SimpleNamespace(PIPE=-1, STDOUT=-2, run=launch)}
    production = _covered_function("production", namespace)
    production("restore", "--repository", "local")
    argv, options = calls[-1]
    assert argv == ["/opt/bbackup/venv/bin/bbackup", "production", "--config",
                    "/etc/bbackup/covered-root/config.v3.json", "--bindings",
                    "/etc/bbackup/covered-root/bindings.v3.json", "restore", "--repository", "local"]
    assert options["env"]["PYTHONPATH"].split(os.pathsep)[0] == str(tmp_path / "diagnostics")
    assert (tmp_path / "diagnostics" / "sitecustomize.py").stat().st_mode & 0o777 == 0o600
    production("coverage", "check")
    assert calls[-1][1]["env"] is None


def test_covered_root_restic_wrapper_records_restore_failure_only(tmp_path):
    tree = ast.parse(COVERED_ROOT_GUEST_STAGE)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "wrapper_script")
    wrapper_literal = next(node.value for node in function.body if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == "wrapper"
                                   for target in node.targets))
    real = tmp_path / "restic.real"
    real.write_text("#!/bin/sh\ncase \" $* \" in *' restore '*) echo 'synthetic restic error' >&2; exit 7;; esac\necho normal\n",
                    encoding="utf-8")
    real.chmod(0o700)
    diagnostic = tmp_path / "diagnostics"
    diagnostic.mkdir(mode=0o700)
    wrapper = tmp_path / "restic"
    wrapper.write_text(ast.literal_eval(wrapper_literal)
        .replace("/usr/local/libexec/bbackup/restic.real", str(real))
        .replace("/var/log/bbackup-covered-root-restore-diagnostics", str(diagnostic)),
        encoding="utf-8")
    wrapper.chmod(0o700)
    normal = subprocess.run([str(wrapper), "snapshots"], capture_output=True, text=True, check=False)
    assert normal.returncode == 0 and normal.stdout.strip() == "normal"
    assert not list(diagnostic.iterdir())
    failed = subprocess.run([str(wrapper), "-r", "fixture", "restore", "snapshot"],
                            capture_output=True, text=True, check=False)
    assert failed.returncode == 7 and failed.stderr.strip() == "synthetic restic error"
    assert (diagnostic / "restic.status").read_text(encoding="utf-8") == "exit_code=7\n"
    assert (diagnostic / "restic.stderr").read_text(encoding="utf-8") == "synthetic restic error\n"
    assert (diagnostic / "restic.stderr").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(("retained", "state", "substate"), [
    (False, "inactive", "dead"),
    (True, "active", "exited"),
])
def test_covered_root_unit_proof_requires_retained_invocation_when_requested(
        retained, state, substate):
    properties = {"Result": "success", "ExecMainStatus": "0",
                  "ExecMainStartTimestamp": "Wed 2026-10-21 05:00:00 UTC",
                  "ExecMainExitTimestamp": "Wed 2026-10-21 05:00:01 UTC",
                  "ActiveState": state, "SubState": substate}
    output = "\n".join(f"{key}={value}" for key, value in properties.items())
    namespace = {"rootctl": lambda *args: SimpleNamespace(stdout=output)}
    unit_success = _covered_function("unit_success", namespace)
    assert unit_success("fixture.service", retained=retained)
    assert not unit_success("fixture.service", retained=not retained)
    properties["ExecMainExitTimestamp"] = ""
    output = "\n".join(f"{key}={value}" for key, value in properties.items())
    assert not unit_success("fixture.service", retained=retained)


def test_covered_root_fixture_retains_successful_oneshot_state():
    unit = _covered_function("app_unit_text", {})()
    assert "Type=oneshot\nRemainAfterExit=yes\n" in unit


def test_existing_reboot_scenario_remains_the_default(monkeypatch):
    from scripts import qualify_systemd as module

    monkeypatch.setattr(module.sys, "argv", ["qualify_systemd.py", "--artifact", "candidate.whl",
        "--wheel-sha256", "a" * 64, "--wheelhouse", "wheels", "--requirements", "requirements.txt",
        "--restic", "restic", "--uv", "uv"])
    assert module._parse_args().scenario == "timer-reboot"
    monkeypatch.setattr(module.sys, "argv", [*module.sys.argv, "--scenario", "covered-root"])
    assert module._parse_args().scenario == "covered-root"


def test_covered_root_seeds_have_distinct_cloud_init_identities(tmp_path, monkeypatch):
    from scripts import qualify_systemd as module

    written = {}

    def write_iso(stage, directory, filename, label):
        target = stage / filename
        target.write_text(label, encoding="ascii")
        written[filename] = (directory / "user-data").read_text(encoding="utf-8")
        return target

    monkeypatch.setattr(module, "_write_iso", write_iso)
    source = module._create_covered_root_seed(tmp_path, "a" * 64, "noble", "source")
    replacement = module._create_covered_root_seed(tmp_path, "a" * 64, "noble", "replacement")
    source_id = (tmp_path / "covered-root-seed-source" / "meta-data").read_text()
    replacement_id = (tmp_path / "covered-root-seed-replacement" / "meta-data").read_text()
    assert source.name != replacement.name
    assert "instance-id: bbackup-covered-root-source-" in source_id
    assert "instance-id: bbackup-covered-root-replacement-" in replacement_id
    assert source_id != replacement_id
    assert "ExecStart=/usr/bin/python3 /usr/local/sbin/bbackup-covered-root-qualification.py source" in written[source.name]
    assert "ExecStart=/usr/bin/python3 /usr/local/sbin/bbackup-covered-root-qualification.py replacement" in written[replacement.name]
    assert "StandardOutput=journal+console" in written[source.name]
    assert "journalctl --no-pager -n 40 -u bbackup-covered-root-qualification.service" in written[source.name]
    assert "systemctl poweroff; exit 1" in written[source.name]


@pytest.mark.parametrize("accelerator", ["kvm", "tcg"])
def test_covered_root_runner_uses_sequential_fresh_overlays_and_shared_recovery(
        tmp_path, monkeypatch, accelerator):
    from scripts import qualify_systemd as module

    commands = []

    def run(args, **kwargs):
        commands.append(args)
        if args[1:3] == ["container", "inspect"]:
            return SimpleNamespace(returncode=1, stdout="")
        (tmp_path / "source-serial.log").write_text("SOURCE_COMPLETE {}\n", encoding="utf-8")
        (tmp_path / "replacement-serial.log").write_text("COVERED_ROOT_RESULT {}\n", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(module, "_run", run)
    result = module._docker_covered_root_qualification(
        tmp_path, tmp_path / "ubuntu.img", tmp_path / "covered-root-seed-source.iso",
        tmp_path / "covered-root-seed-replacement.iso", tmp_path / "inputs.iso", 900,
        accelerator=accelerator)
    command = commands[-1]
    shell = command[-1]
    assert "SOURCE_COMPLETE" in result and "COVERED_ROOT_RESULT" in result
    assert "--device=/dev/kvm:/dev/kvm:rwm" in command if accelerator == "kvm" else "--device=/dev/kvm:/dev/kvm:rwm" not in command
    assert "qemu-img create -q -f qcow2 -F qcow2 -b /ubuntu.img /work/source-guest.qcow2 16G" in shell
    assert "qemu-img create -q -f qcow2 -F qcow2 -b /ubuntu.img /work/replacement-guest.qcow2 16G" in shell
    source_command = shell.split(" > /work/source-serial.log", 1)[0].rsplit("; ", 1)[-1]
    replacement_command = shell.split(" > /work/replacement-serial.log", 1)[0].rsplit("; ", 1)[-1]
    assert "source-guest.qcow2" in source_command and "replacement-guest.qcow2" not in source_command
    assert "replacement-guest.qcow2" in replacement_command and "source-guest.qcow2" not in replacement_command
    assert "recovery.raw" in source_command and "recovery.raw" in replacement_command
    assert "covered-root-seed-source.iso" in source_command
    assert "covered-root-seed-replacement.iso" in replacement_command
    assert source_command.count("inputs.iso") == 1 and replacement_command.count("inputs.iso") == 1
    assert f"accel={accelerator}" in shell
    assert "-serial stdio" in shell and "-display none" in shell
    assert "grep -Fq 'SOURCE_COMPLETE {' /work/source-serial.log" in shell
    assert shell.index("grep -Fq 'SOURCE_COMPLETE {'") < shell.index("replacement-serial.log")


def test_covered_root_runner_leaves_foreign_owned_artifact_modes_alone(tmp_path, monkeypatch):
    from scripts import qualify_systemd as module

    apk = tmp_path / "apk-installed.txt"
    apk.write_text("installed packages", encoding="utf-8")
    apk.chmod(0o664)
    source = tmp_path / "source-serial.log"
    source.write_text("SOURCE_COMPLETE {}\n", encoding="utf-8")
    source.chmod(0o664)
    original_euid = os.geteuid()
    monkeypatch.setattr(module.os, "geteuid", lambda: original_euid + 1)

    def run(args, **kwargs):
        return SimpleNamespace(returncode=1 if args[1:3] == ["container", "inspect"] else 0,
                               stdout="")

    monkeypatch.setattr(module, "_run", run)
    module._docker_covered_root_qualification(
        tmp_path, tmp_path / "ubuntu.img", tmp_path / "covered-root-seed-source.iso",
        tmp_path / "covered-root-seed-replacement.iso", tmp_path / "inputs.iso", 900)
    assert apk.stat().st_mode & 0o777 == 0o664
    assert source.stat().st_mode & 0o777 == 0o664


@pytest.mark.parametrize("source_complete", [False, True])
def test_covered_root_shell_starts_replacement_only_after_source_proof(
        tmp_path, monkeypatch, source_complete):
    from scripts import qualify_systemd as module

    commands = []

    def run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=1 if args[1:3] == ["container", "inspect"] else 0,
                               stdout="")

    monkeypatch.setattr(module, "_run", run)
    module._docker_covered_root_qualification(
        tmp_path, tmp_path / "ubuntu.img", tmp_path / "covered-root-seed-source.iso",
        tmp_path / "covered-root-seed-replacement.iso", tmp_path / "inputs.iso", 900)
    shell = commands[-1][-1]
    source_qemu = shell.split(" > /work/source-serial.log 2>&1", 1)[0].rsplit("; ", 1)[-1]
    replacement_qemu = shell.split(" > /work/replacement-serial.log 2>&1", 1)[0].rsplit("; ", 1)[-1]
    source_log = tmp_path / "source-serial.log"
    replacement_marker = tmp_path / "replacement-started"
    script = shell[shell.index(source_qemu):]
    script = script.replace(source_qemu + " > /work/source-serial.log 2>&1",
                            "printf '" + ("SOURCE_COMPLETE {}" if source_complete else "BOOT_ROLE source")
                            + "\\n' > " + shlex.quote(str(source_log)), 1)
    script = script.replace(replacement_qemu + " > /work/replacement-serial.log 2>&1",
                            "touch " + shlex.quote(str(replacement_marker)), 1)
    script = script.replace("/work/source-serial.log", shlex.quote(str(source_log)))
    result = subprocess.run(["sh", "-ec", script], capture_output=True, text=True, check=False)
    assert result.returncode == (0 if source_complete else 22)
    assert replacement_marker.exists() is source_complete


@pytest.mark.parametrize("accelerator", ["kvm", "tcg"])
def test_guest_runner_uses_only_selected_accelerator(tmp_path, monkeypatch, accelerator):
    from scripts import qualify_systemd as module

    commands = []

    def run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=1 if args[1:3] == ["container", "inspect"] else 0,
                               stdout="guest proof")

    monkeypatch.setattr(module, "_run", run)
    assert module._docker_qualification(tmp_path, tmp_path / "image", tmp_path / "seed",
        tmp_path / "inputs", 600, accelerator=accelerator) == "guest proof"
    command = commands[-1]
    assert ("--device=/dev/kvm:/dev/kvm:rwm" in command) == (accelerator == "kvm")
    assert f"accel={accelerator}" in command[-1]
    assert ("-cpu host" in command[-1]) == (accelerator == "kvm")
    assert "-display none" in command[-1]


@pytest.mark.parametrize(("changes", "expected"), [
    ({}, True),
    ({"ActiveState": "active", "SubState": "running"}, False),
    ({"ExecMainExitTimestamp": ""}, False),
    ({"ExecMainStartTimestamp": ""}, False),
    ({"Result": "exit-code", "ExecMainStatus": "1"}, False),
])
def test_guest_success_requires_completed_successful_invocation(changes, expected):
    properties = {"Result": "success", "ExecMainStatus": "0", "ActiveState": "inactive",
                  "SubState": "dead", "ExecMainStartTimestamp": "2026-10-17 00:00:00 UTC",
                  "ExecMainExitTimestamp": "2026-10-17 00:00:05 UTC", **changes}
    observed, _ = _predicate("service_ok", "\n".join(f"{key}={value}" for key, value in properties.items()))
    assert observed is expected


@pytest.mark.parametrize(("value", "expected"), [("", False), ("0", False), ("n/a", False),
                                                 ("2026-10-17 00:00:00 UTC", True)])
def test_guest_timer_requires_real_documented_trigger_property(value, expected):
    observed, calls = _predicate("timer_triggered", f"LastTriggerUSec={value}\n")
    assert observed is expected
    assert calls[0][-1] == "LastTriggerUSec"
