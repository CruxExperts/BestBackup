"""Render disabled systemd units; enrollment and activation are explicit."""
from pathlib import Path

from .models import Job


def _argument(path: Path) -> str:
    value = str(path)
    if not path.is_absolute() or any(c in value for c in '\0\n\r'):
        raise ValueError("Unit paths must be absolute single-line paths")
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def job_units(job: Job, *, executable: Path, config: Path, bindings: Path) -> dict[str, str]:
    # Names and calendar expressions are validated by the configuration owner.
    command = " ".join((_argument(executable), "production", "--config", _argument(config),
                        "--bindings", _argument(bindings), "jobs", "run", "--name", job.name))
    return {
        f"bbackup-{job.name}.service": (
            "[Unit]\nDescription=bbackup scheduled capture\nWants=network-online.target\n"
            "After=network-online.target\n\n[Service]\nType=exec\n"
            f"ExecStart={command}\nUMask=0077\nKillMode=control-group\n"
            "TimeoutStopSec=30\nNoNewPrivileges=yes\nPrivateTmp=yes\n"
        ),
        f"bbackup-{job.name}.timer": (
            "[Unit]\nDescription=bbackup capture schedule\n\n[Timer]\n"
            f"OnCalendar={job.schedule.replace('%', '%%')}\nPersistent=true\n"
            f"Unit=bbackup-{job.name}.service\n\n[Install]\nWantedBy=timers.target\n"
        ),
    }
