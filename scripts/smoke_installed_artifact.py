"""
Smoke test a built wheel in an isolated environment.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def run(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60)


def assert_ok(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = run(command, env=env)
    if result.returncode != 0:
        sys.stderr.write(f"smoke: command failed: {' '.join(command)}\n")
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    return result


def latest_wheel(dist_dir: Path) -> Path:
    wheels = sorted(dist_dir.glob("bbackup-*.whl"))
    if not wheels:
        raise SystemExit("smoke: no bbackup wheel found in dist/")
    return wheels[-1]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Smoke test a built bbackup wheel.")
    parser.add_argument("--wheel", type=Path, default=None, help="Wheel path. Defaults to latest dist/bbackup-*.whl.")
    args = parser.parse_args(argv)

    wheel = args.wheel or latest_wheel(REPO_ROOT / "dist")
    if not wheel.exists():
        raise SystemExit(f"smoke: wheel not found: {wheel}")

    with tempfile.TemporaryDirectory(prefix="bbackup-wheel-smoke-") as tmp:
        smoke_root = Path(tmp)
        venv = smoke_root / ".venv"
        env = {
            "HOME": str(smoke_root),
        }

        assert_ok(["uv", "venv", str(venv), "--python", "3.12"])
        requirements = smoke_root / "locked-requirements.txt"
        exported = assert_ok(["uv", "export", "--locked", "--no-dev", "--no-emit-project", "--format", "requirements-txt"])
        requirements.write_text(exported.stdout, encoding="utf-8")
        assert_ok(["uv", "pip", "install", "--python", str(venv / "bin" / "python"),
                   "--require-hashes", "-r", str(requirements)])
        assert_ok(["uv", "pip", "install", "--python", str(venv / "bin" / "python"), "--no-deps", str(wheel)])

        bin_dir = venv / "bin"
        bbackup = str(bin_dir / "bbackup")
        bbman = str(bin_dir / "bbman")

        assert_ok([bbackup, "--version"], env=env)
        assert_ok([bbman, "--version"], env=env)
        production = assert_ok([bbackup, "production", "skills", "--name", "jobs run"], env=env)
        production_payload = json.loads(production.stdout)
        if production_payload.get("schema_version") != 2 or "jobs run" not in production_payload.get("data", {}):
            raise SystemExit("smoke: installed production schema discovery failed")

        init_result = assert_ok([bbackup, "init-config", "--output", "json"], env=env)
        init_payload = json.loads(init_result.stdout)
        if not init_payload.get("success"):
            raise SystemExit("smoke: init-config did not report success")

        config_path = smoke_root / ".config" / "bbackup" / "config.yaml"
        if not config_path.exists():
            raise SystemExit(f"smoke: init-config did not create {config_path}")

        skills_result = assert_ok([bbackup, "skills", "--format", "markdown"], env=env)
        if "# CLI skills catalog" not in skills_result.stdout:
            raise SystemExit("smoke: bbackup skills markdown missing catalog header")

        bbman_skills_result = assert_ok([bbman, "skills", "--format", "markdown"], env=env)
        if "# CLI skills catalog" not in bbman_skills_result.stdout:
            raise SystemExit("smoke: bbman skills markdown missing catalog header")

        command_skills_result = assert_ok([bbackup, "init-config", "--skills"], env=env)
        if "### bbackup init-config" not in command_skills_result.stdout:
            raise SystemExit("smoke: bbackup init-config --skills missing command section")

        repo_url_result = assert_ok([bbman, "repo-url", "--output", "json"], env=env)
        if "https://github.com/CruxExperts/best-backup" not in repo_url_result.stdout:
            raise SystemExit("smoke: bbman repo-url did not report canonical repository")

    print(f"smoke: ok ({wheel})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
