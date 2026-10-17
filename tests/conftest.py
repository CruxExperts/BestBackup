"""
Shared pytest fixtures for bbackup unit tests.
Purpose: Provide mock_docker_client, mock_subprocess, mock_requests_head, and sample_config_yaml
         to all unit test files. No test file should re-patch these targets independently.
Created: 2026-02-26
Last Updated: 2026-02-26
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def pytest_xdist_auto_num_workers(config: pytest.Config) -> int:
    """Use one third of CPUs available through affinity and cgroup quota."""
    del config
    try:
        available = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        available = os.cpu_count() or 1
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            available = min(available, max(1, int(quota) // int(period)))
    except (OSError, ValueError, ZeroDivisionError):
        pass
    return max(1, available // 3)


@pytest.fixture
def mock_docker_client():
    """Patches all three docker.from_env call sites in bbackup."""
    with patch("bbackup.docker_backup.docker.from_env") as m1, \
         patch("bbackup.restore.docker.from_env") as m2, \
         patch("bbackup.management.health.docker.from_env") as m3:
        client = MagicMock()
        client.ping.return_value = True
        client.version.return_value = {"Version": "24.0.0"}
        m1.return_value = client
        m2.return_value = client
        m3.return_value = client
        yield client


@pytest.fixture
def mock_subprocess():
    """Patches subprocess.run and subprocess.Popen globally."""
    with patch("subprocess.run") as mock_run, \
         patch("subprocess.Popen") as mock_popen:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        yield mock_run, mock_popen


@pytest.fixture
def mock_requests_head():
    """Patches requests.head inside bbackup.encryption for GitHub shortcut resolution."""
    with patch("bbackup.encryption.requests.head") as mock_head:
        mock_head.return_value = MagicMock(status_code=404)
        yield mock_head


@pytest.fixture
def sample_config_yaml(tmp_path):
    """Write a minimal valid config YAML to tmp_path and return the path."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("""
backup:
  staging_dir: /tmp/bbackup_test
  retention:
    daily: 7
    weekly: 4
    monthly: 3
remotes: []
encryption:
  enabled: false
""")
    return cfg
