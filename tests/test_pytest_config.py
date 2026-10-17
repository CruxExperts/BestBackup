"""Tests for repository pytest execution policy."""

import importlib
import os
from pathlib import Path
from collections.abc import Callable
from typing import cast

import pytest


worker_count = cast(
    Callable[[object], int],
    getattr(importlib.import_module("tests.conftest"), "pytest_xdist_auto_num_workers"),
)


@pytest.mark.parametrize(
    ("cpus", "expected"),
    [
        (None, 1),
        (1, 1),
        (2, 1),
        (3, 1),
        (12, 4),
        (15, 5),
        (16, 5),
        (64, 21),
    ],
)
def test_xdist_worker_count_respects_available_cpu_budget(
    monkeypatch: pytest.MonkeyPatch,
    cpus: int | None,
    expected: int,
) -> None:
    monkeypatch.setattr(os, "cpu_count", lambda: cpus)
    def affinity(_pid):
        if cpus is None:
            raise OSError("No affinity API")
        return set(range(cpus))
    monkeypatch.setattr(os, "sched_getaffinity", affinity)
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: "max 100000")

    assert worker_count(None) == expected


def test_cgroup_cpu_quota_limits_parallel_tests(monkeypatch):
    monkeypatch.setattr(os, "sched_getaffinity", lambda _pid: set(range(24)))
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: "600000 100000")
    assert worker_count(None) == 2
