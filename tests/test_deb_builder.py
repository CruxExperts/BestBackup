from __future__ import annotations

import importlib.util
import shutil
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "scripts" / "build_deb.py"
SPEC = importlib.util.spec_from_file_location("bbackup_build_deb_export_test", BUILDER_PATH)
assert SPEC is not None and SPEC.loader is not None
BUILDER_MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BUILDER_MODULE
SPEC.loader.exec_module(BUILDER_MODULE)


class _BuilderModule(Protocol):
    ROOT: Path
    export_locked_requirements: Callable[[str, Path, Path, str], str]
    sha256_file: Callable[[Path], str]


BUILDER = cast(_BuilderModule, cast(object, BUILDER_MODULE))


def test_export_locked_requirements_uses_builder_python_when_project_pin_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uv = shutil.which("uv")
    assert uv is not None, "uv is required to build native packages"

    project = tmp_path / "project"
    _ = project.mkdir()
    for name in ("README.md", "pyproject.toml", "uv.lock"):
        _ = shutil.copy2(ROOT / name, project / name)
    _ = (project / ".python-version").write_text("3.99\n", encoding="ascii")

    interpreter_dir = tmp_path / "bin"
    _ = interpreter_dir.mkdir()
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    for name in ("python", "python3", f"python{python_version}"):
        _ = (interpreter_dir / name).symlink_to(sys.executable)

    monkeypatch.setenv("PATH", str(interpreter_dir))
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "uv-cache"))
    monkeypatch.setenv("UV_NO_MANAGED_PYTHON", "1")
    monkeypatch.setenv("UV_OFFLINE", "1")
    monkeypatch.delenv("UV_PYTHON", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.setattr(BUILDER, "ROOT", project)

    lock_path = project / "uv.lock"
    requirements_path = tmp_path / "requirements.txt"
    export_hash = BUILDER.export_locked_requirements(
        uv,
        lock_path,
        requirements_path,
        BUILDER.sha256_file(lock_path),
    )

    requirements = requirements_path.read_text(encoding="utf-8")
    assert "rich==" in requirements.lower()
    assert "--hash=sha256:" in requirements
    assert export_hash == BUILDER.sha256_file(requirements_path)
