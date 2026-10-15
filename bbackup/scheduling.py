"""Render disabled systemd units; enrollment and activation are explicit."""
from pathlib import Path

from .models import Job


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
