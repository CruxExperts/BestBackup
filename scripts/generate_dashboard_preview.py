"""Capture the real dashboard headlessly with disposable, sanitized example data."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

from bbackup.configuration_workspace import ConfigurationWorkspace
from bbackup.dashboard import BackupDashboard
from bbackup.production_operations import execute_operation

ROOT = Path(__file__).resolve().parent.parent
RIGHTS = ("Copyright © 2026 Crux Experts LLC. https://www.cruxexperts.com/ "
          "Private asset. Property of Crux Experts LLC. Use, reproduction, modification, "
          "distribution, or publication without prior written permission is prohibited.")


async def generate() -> None:
    with tempfile.TemporaryDirectory(prefix="bbackup-preview-") as temporary:
        root = Path(temporary)
        source = root / "documents"
        source.mkdir()
        (source / "welcome.txt").write_text("A verified example restore.\n")
        repositories = {}
        for name in ("local", "additional-copy"):
            password = root / f"{name}.password"
            password.write_text(f"disposable-example-{name}")
            password.chmod(0o600)
            repositories[name] = {"repository": str(root / name), "password_file": str(password)}
        workspace = ConfigurationWorkspace(root / "config.json", root / "bindings.json")
        service = workspace.setup(json.dumps({"schema_version": 3,
            "repositories": [{"name": "local"}, {"name": "additional-copy", "role": "replica"}],
            "sources": [{"name": "documents", "kind": "files"}],
            "jobs": [{"name": "daily", "repository": "local", "sources": ["documents"],
                      "replicas": ["additional-copy"]}]}), json.dumps({
                "schema_version": 3, "state_dir": str(root / "state"), "repositories": repositories,
                "source_bindings": {"documents": {"paths": [str(source)]}}}))
        def run(command, values):
            return execute_operation(command, values, config_path=workspace.config_path,
                                     bindings_path=workspace.bindings_path)
        for name in repositories:
            run("repositories init", {"name": name})
        result = run("jobs run", {"name": "daily"})
        run("repositories check", {"name": "local"})
        run("restore", {"repository": "local", "snapshot": result["snapshot_id"],
                        "target": str(root / "restored")})
        app = BackupDashboard(service, workspace=workspace)
        # Capacity is explicitly illustrative; never publish host disk measurements.
        example_capacity = shutil._ntuple_diskusage(512 * 1024**3, 128 * 1024**3, 384 * 1024**3)
        with patch("bbackup.service_status.shutil.disk_usage", return_value=example_capacity):
            async with app.run_test(size=(112, 36)) as pilot:
                await pilot.pause()
                await app.workers.wait_for_complete()
                svg = app.export_screenshot(title="bbackup • disposable example data")
        svg = svg.replace("\n", f"\n<metadata>{RIGHTS} Real headless fixture; capacity is illustrative.</metadata>\n", 1)
        output = ROOT / "docs/assets/bbackup-dashboard-preview.svg"
        output.write_text("\n".join(line.rstrip() for line in svg.splitlines()) + "\n")
        print(output)


if __name__ == "__main__":
    asyncio.run(generate())
