"""Exercise guest evidence predicates without starting a VM or host service."""
import ast
from types import SimpleNamespace

import pytest

from scripts.qualify_systemd import GUEST_STAGE


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
