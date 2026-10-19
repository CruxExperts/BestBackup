from pathlib import Path

import pytest

from bbackup.models import Job
from bbackup.scheduling import install_user_units, job_units, unit_templates


def test_unit_templates_are_disabled_and_use_secure_daily_defaults() -> None:
    units = unit_templates()

    assert set(units) == {"bbackup@.service", "bbackup@.timer"}
    service = units["bbackup@.service"]
    expected_command = (
        'ExecStart="/usr/bin/bbackup" production --config "/etc/bbackup/config.json" '
        '--bindings "/etc/bbackup/bindings.json" jobs run --name %i'
    )
    assert expected_command in service
    assert "UMask=0077" in service
    assert "KillMode=control-group" in service
    assert "NoNewPrivileges=yes" in service
    assert "PrivateTmp=yes" in service
    assert "[Install]" not in service

    timer = units["bbackup@.timer"]
    assert "OnCalendar=daily" in timer
    assert "Persistent=true" in timer
    assert "Unit=bbackup@%i.service" in timer
    assert "WantedBy=timers.target" in timer


def test_template_paths_use_existing_systemd_argument_escaping() -> None:
    units = unit_templates(
        executable=Path('/opt/bbackup tool/runner"%'),
        config=Path('/etc/bbackup/config with space.json'),
        bindings=Path('/etc/bbackup/bindings.json'),
    )

    command = units["bbackup@.service"]
    assert '"/opt/bbackup tool/runner\\"%%"' in command
    assert '"/etc/bbackup/config with space.json"' in command

    with pytest.raises(ValueError, match="absolute single-line"):
        unit_templates(executable=Path("relative/bbackup"))


def test_job_renderer_keeps_custom_schedule_and_shared_service_hardening() -> None:
    units = job_units(
        Job(name="nightly", repository="primary", sources=("home",), schedule="weekly"),
        executable=Path("/usr/bin/bbackup"),
        config=Path("/etc/bbackup/config.json"),
        bindings=Path("/etc/bbackup/bindings.json"),
    )

    assert "OnCalendar=weekly" in units["bbackup-nightly.timer"]
    assert "Unit=bbackup-nightly.service" in units["bbackup-nightly.timer"]
    assert "NoNewPrivileges=yes" in units["bbackup-nightly.service"]


def test_user_install_requires_explicit_activation_and_preserves_existing_units(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    commands = []
    options = []
    def run(args, **kwargs):
        commands.append(args)
        options.append(kwargs)
    monkeypatch.setattr("bbackup.scheduling.subprocess.run", run)
    units = job_units(
        Job(name="nightly", repository="primary", sources=("home",)),
        executable=Path("/usr/bin/bbackup"), config=Path("/etc/bbackup/config.json"),
        bindings=Path("/etc/bbackup/bindings.json"),
    )
    result = install_user_units(units)
    assert result["activated"] is False
    assert commands == [["systemctl", "--user", "daemon-reload"]]
    directory = tmp_path / ".config" / "systemd" / "user"
    assert directory.stat().st_mode & 0o077 == 0
    assert (directory / "bbackup-nightly.service").read_text() == units["bbackup-nightly.service"]
    commands.clear()
    result = install_user_units(units, activate=True)
    assert result["installed"] == []
    assert all(kwargs.get("capture_output") and kwargs.get("text") for kwargs in options)
    assert commands[-1] == ["systemctl", "--user", "enable", "--now", "bbackup-nightly.timer"]
    (directory / "bbackup-nightly.timer").write_text("operator content")
    commands.clear()
    with pytest.raises(ValueError, match="differs"):
        install_user_units(units, activate=True)
    assert commands == []


@pytest.mark.parametrize("xdg", ["absolute", "relative", "empty"])
def test_user_install_honors_absolute_xdg_and_ignores_invalid_relative_value(tmp_path, monkeypatch, xdg):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    config = tmp_path / "config" if xdg == "absolute" else tmp_path / "home" / ".config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config) if xdg == "absolute" else "relative" if xdg == "relative" else "")
    monkeypatch.setattr("bbackup.scheduling.subprocess.run", lambda *args, **kwargs: None)
    units = job_units(Job(name="daily", repository="primary", sources=("home",)),
                      executable=Path("/usr/bin/bbackup"), config=Path("/etc/config.json"),
                      bindings=Path("/etc/bindings.json"))
    install_user_units(units)
    assert (config / "systemd/user/bbackup-daily.timer").read_text() == units["bbackup-daily.timer"]
    assert (config / "systemd/user").stat().st_mode & 0o077 == 0


def test_user_install_rejects_symlink_inside_custom_xdg_ancestor(tmp_path, monkeypatch):
    actual = tmp_path / "actual"
    actual.mkdir()
    (tmp_path / "alias").symlink_to(actual, target_is_directory=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "alias" / "config"))
    commands = []
    monkeypatch.setattr("bbackup.scheduling.subprocess.run", lambda *a, **kw: commands.append(a))
    units = job_units(Job(name="daily", repository="primary", sources=("home",)),
                      executable=Path("/usr/bin/bbackup"), config=Path("/etc/config.json"),
                      bindings=Path("/etc/bindings.json"))
    with pytest.raises(ValueError, match="without symbolic links"):
        install_user_units(units)
    assert not commands
    assert not (actual / "config").exists()


def test_user_install_rejects_parent_traversal_in_custom_xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "alias" / ".." / "config"))
    commands = []
    monkeypatch.setattr("bbackup.scheduling.subprocess.run", lambda *a, **kw: commands.append(a))
    units = job_units(Job(name="daily", repository="primary", sources=("home",)),
                      executable=Path("/usr/bin/bbackup"), config=Path("/etc/config.json"),
                      bindings=Path("/etc/bindings.json"))
    with pytest.raises(ValueError, match="parent traversal"):
        install_user_units(units)
    assert not commands
