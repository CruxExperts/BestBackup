"""Render disabled systemd units; enrollment and activation are explicit."""
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .models import Job
from .config import ensure_private_directory


def _argument(path: Path) -> str:
    value = str(path)
    if not path.is_absolute() or any(c in value for c in '\0\n\r'):
        raise ValueError("Unit paths must be absolute single-line paths")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def _service_unit(command: str) -> str:
    return (
        "[Unit]\nDescription=bbackup scheduled capture\nWants=network-online.target\n"
        "After=network-online.target\n\n[Service]\nType=exec\n"
        f"ExecStart={command}\nUMask=0077\nKillMode=control-group\n"
        "TimeoutStopSec=30\nNoNewPrivileges=yes\nPrivateTmp=yes\n"
    )


def _timer_unit(schedule: str, service_unit: str, description: str) -> str:
    return (
        f"[Unit]\nDescription={description}\n\n[Timer]\n"
        f"OnCalendar={schedule.replace('%', '%%')}\nPersistent=true\n"
        f"Unit={service_unit}\n\n[Install]\nWantedBy=timers.target\n"
    )


def job_units(job: Job, *, executable: Path, config: Path, bindings: Path) -> dict[str, str]:
    # Names and calendar expressions are validated by the configuration owner.
    command = " ".join((_argument(executable), "production", "--config", _argument(config),
                        "--bindings", _argument(bindings), "jobs", "run", "--name", job.name))
    service_unit = f"bbackup-{job.name}.service"
    return {
        service_unit: _service_unit(command),
        f"bbackup-{job.name}.timer": _timer_unit(
            job.schedule, service_unit, "bbackup capture schedule"
        ),
    }


def unit_templates(
    *,
    executable: Path = Path("/usr/bin/bbackup"),
    config: Path = Path("/etc/bbackup/config.json"),
    bindings: Path = Path("/etc/bbackup/bindings.json"),
) -> dict[str, str]:
    """Return disabled systemd templates; enrollment and activation remain explicit."""
    command = " ".join(
        (
            _argument(executable),
            "production",
            "--config",
            _argument(config),
            "--bindings",
            _argument(bindings),
            "jobs",
            "run",
            "--name",
            "%i",
        )
    )
    return {
        "bbackup@.service": _service_unit(command),
        "bbackup@.timer": _timer_unit(
            "daily", "bbackup@%i.service", "bbackup daily capture schedule"
        ),
    }


def install_user_units(units: dict[str, str], *, activate: bool = False) -> dict[str, object]:
    """Install one bbackup job's units in the current user's private systemd tree."""
    if type(activate) is not bool or len(units) != 2:
        raise ValueError("Expected one service and one timer with explicit activation")
    services = [name for name in units if name.endswith(".service")]
    if len(services) != 1:
        raise ValueError("Expected one bbackup service and matching timer")
    service_name = services[0]
    if not re.fullmatch(r"bbackup-[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\.service", service_name):
        raise ValueError("Invalid bbackup service unit name")
    timer_name = service_name.removesuffix(".service") + ".timer"
    if set(units) != {service_name, timer_name}:
        raise ValueError("Expected one matching bbackup service and timer")
    if any(not isinstance(content, str) or "\0" in content or not content
           for content in units.values()):
        raise ValueError("Invalid systemd unit content")
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not config_home.is_absolute():
        config_home = Path.home() / ".config"
    if ".." in config_home.parts:
        raise ValueError("User systemd path must not contain parent traversal")
    directory = config_home / "systemd" / "user"
    try:
        ensure_private_directory(directory)
    except OSError as error:
        raise ValueError("User systemd path must use trusted directories without symbolic links") from error
    for name, content in units.items():
        target = directory / name
        if target.exists() or target.is_symlink():
            if target.is_symlink() or not stat.S_ISREG(target.stat().st_mode) or target.read_text(encoding="utf-8") != content:
                raise ValueError(f"Existing {name} differs; review it before installation")
    installed = []
    for name, content in units.items():
        target = directory / name
        if target.exists():
            continue
        descriptor, temporary_name = tempfile.mkstemp(prefix=".bbackup-unit-", dir=directory)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
            installed.append(name)
        finally:
            temporary.unlink(missing_ok=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, timeout=15, capture_output=True, text=True)
        if activate:
            subprocess.run(["systemctl", "--user", "enable", "--now", timer_name], check=True, timeout=30, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise ValueError("User systemd did not accept the installed units; review user service status") from None
    return {"installed": installed, "service_unit": service_name,
            "timer_unit": timer_name, "activated": activate}
