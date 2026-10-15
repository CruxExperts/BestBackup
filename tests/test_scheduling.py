from pathlib import Path

import pytest

from bbackup.models import Job
from bbackup.scheduling import job_units, unit_templates


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
