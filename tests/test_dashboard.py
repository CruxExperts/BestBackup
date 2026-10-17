# ruff: noqa: F811
import asyncio
import json
import shutil

import pytest

from textual.widgets import Button, DataTable, Input

from bbackup.dashboard import BackupDashboard
from bbackup.configuration_workspace import ConfigurationWorkspace
from tests.test_service import service  # noqa: F401


def test_mouse_keyboard_search_and_small_screen(service):
    async def exercise():
        app = BackupDashboard(service)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.click("#--content-tab-jobs")
            await pilot.pause()
            table = app.query_one("#job-table", DataTable)
            assert table.row_count == 1
            table.focus()
            await pilot.press("enter")
            assert app.selected_job == "daily"
            assert not app.query_one("#start", Button).disabled
            await pilot.click("#job-search")
            await pilot.press("n", "o", "p", "e")
            assert table.row_count == 0
            assert app.query_one("#start", Button).disabled
            await pilot.resize_terminal(60, 20)
            app.query_one("#job-search", Input).value = ""
            await pilot.pause()
            assert table.row_count == 1
            await pilot.press("escape")
    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.skipif(shutil.which("restic") is None, reason="restic is required")
def test_dashboard_restore_verifies_files_and_refuses_existing_target(service, tmp_path):
    from textual.widgets import Static, TabbedContent

    service.initialize("local")
    snapshot = service.capture("daily")["snapshot_id"]
    workspace = ConfigurationWorkspace(tmp_path / "config.json", tmp_path / "bindings.json")
    service = workspace.setup(json.dumps({"schema_version": 3,
        "repositories": [{"name": "local"}, {"name": "copy", "role": "replica"}],
        "sources": [{"name": "files", "kind": "files"}],
        "jobs": [{"name": "daily", "repository": "local", "sources": ["files"], "replicas": ["copy"]}]}),
        json.dumps({"schema_version": 3, "state_dir": str(service.state),
            "repositories": {name: {"repository": binding.repository, "password_file": str(binding.password_file)}
                             for name, binding in service.bindings.repositories.items()},
            "source_bindings": {"files": {"paths": [str(tmp_path / "data")]}}}))

    async def exercise():
        app = BackupDashboard(service, workspace=workspace)
        async with app.run_test(size=(120, 40)) as pilot:
            app.query_one(TabbedContent).active = "restore"
            await pilot.pause()
            app.query_one("#restore-repository", Input).value = "local"
            app.query_one("#restore-snapshot", Input).value = snapshot
            target = tmp_path / "restored"
            app.query_one("#restore-target", Input).value = str(target)
            await pilot.click("#restore-start")
            await app.workers.wait_for_complete()
            restored = target / str(tmp_path / "data").lstrip("/") / "hello.txt"
            assert restored.read_text() == "restore me\n"
            assert restored.stat().st_mode & 0o777 == 0o640
            assert (restored.parent / "link").is_symlink()
            restored.write_text("operator change\n")
            await pilot.click("#restore-start")
            await app.workers.wait_for_complete()
            assert restored.read_text() == "operator change\n"
            assert "Existing paths are refused" in str(app.query_one("#restore-status", Static).render())
            assert app.query_one("#restore-cancel", Button).disabled
            assert not app.query_one("#restore-start", Button).disabled
            app.query_one(TabbedContent).active = "settings"
            await pilot.pause()
            original = workspace.config_path.read_bytes()
            app.query_one("#settings-timeout", Input).value = "0"
            await app._save_settings()
            assert workspace.config_path.read_bytes() == original
            assert app.query_one("#settings-timeout", Input).value == "0"
            app.query_one("#settings-timeout", Input).value = "600"
            await app._save_settings()
            assert json.loads(workspace.config_path.read_text())["jobs"][0]["timeout_seconds"] == 600
            assert app.service.config.jobs[0].timeout_seconds == 600
            current = workspace.read()
            external = json.loads(current["document"])
            external["jobs"][0]["timeout_seconds"] = 900
            workspace.save(json.dumps(external), current["revision"])
            app._load_settings()
            assert app.query_one("#settings-timeout", Input).value == "900"
            await app._save_settings()
            assert json.loads(workspace.config_path.read_text())["jobs"][0]["timeout_seconds"] == 900

    asyncio.run(exercise())


@pytest.mark.parametrize("size", [(80, 24), (112, 32)])
def test_guided_setup_preserves_invalid_input_and_creates_enrolled_policy(tmp_path, monkeypatch, size):
    from pathlib import Path
    from textual.widgets import Static
    from bbackup.dashboard import BackupSetup
    from bbackup.enrollment import verify

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    workspace = ConfigurationWorkspace(tmp_path / "config.json", tmp_path / "bindings.json")

    async def exercise():
        app = BackupSetup(workspace)
        async with app.run_test(size=size) as pilot:
            await pilot.click("#setup-next")
            assert app._step == 0
            assert "existing absolute source" in str(app.query_one("#setup-status", Static).render())
            app.query_one("#setup-source", Input).value = str(source)
            app.query_one("#setup-source", Input).focus()
            await pilot.press("f1")
            assert "Hidden files" in str(app.query_one("#setup-status", Static).render())
            for _ in range(3):
                app.query_one("#setup-next", Button).focus()
                await pilot.press("enter")
                await pilot.pause(0.35)
            assert app._step == 3, str(app.query_one("#setup-status", Static).render())
            assert str(source) in str(app.query_one("#setup-review", Static).render())
            app.query_one("#setup-save", Button).focus()
            await pilot.press("enter")
            await pilot.pause()
        assert app.return_value is not None
    asyncio.run(exercise())
    policy = json.loads(workspace.config_path.read_text())
    private = json.loads(workspace.bindings_path.read_text())
    assert policy["schema_version"] == 3
    assert "paths" not in policy["sources"][0]
    assert private["source_bindings"]["files"]["paths"] == [str(source)]
    assert (tmp_path / ".config/bbackup/local.password").stat().st_mode & 0o777 == 0o600
    verify(tmp_path / "state", workspace.config_path, workspace.bindings_path)


def test_typed_operations_require_inputs_and_prepare_selected_job(service):
    from textual.widgets import Select, Static
    from bbackup.dashboard_operations import OperationPanel

    async def exercise():
        app = BackupDashboard(service)
        async with app.run_test(size=(112, 32)) as pilot:
            await pilot.pause()
            panel = app.query_one("#job-operations", OperationPanel)
            assert panel.query_one("#operation-run", Button).disabled
            panel.prepare("jobs run", {"name": "daily"})
            await pilot.pause()
            assert panel.query_one("#choice-name", Select).value == "daily"
            assert not panel.query_one("#operation-run", Button).disabled
            assert "Ready" in str(panel.query_one("#operation-status", Static).render())
            assert panel.query_one("#choice-name", Select).tooltip
    asyncio.run(exercise())



def test_dashboard_job_shortcuts_use_shared_operation_panel(service, monkeypatch):
    from bbackup.dashboard_operations import OperationPanel
    calls = []
    monkeypatch.setattr(OperationPanel, "start", lambda panel: calls.append((panel._command, panel._form_values())))
    async def exercise():
        app = BackupDashboard(service)
        async with app.run_test() as pilot:
            app.selected_job = "daily"
            await app._supervise("start")
            await app._supervise("stop")
            await pilot.pause()
    asyncio.run(exercise())
    assert calls == [("jobs run", {"name": "daily"})]


def test_invalid_existing_setup_cannot_enroll_without_a_successful_review(tmp_path, monkeypatch):
    from bbackup.dashboard import BackupSetup
    workspace = ConfigurationWorkspace(tmp_path / "config.json", tmp_path / "bindings.json")
    workspace.config_path.write_text("{}")
    workspace.bindings_path.write_text("{}")
    workspace.bindings_path.chmod(0o600)
    monkeypatch.setattr(workspace, "enroll", lambda **kwargs: pytest.fail("Unreviewed files were enrolled"))
    async def exercise():
        app = BackupSetup(workspace)
        async with app.run_test(size=(100, 32)) as pilot:
            assert app._review_revision is None
            assert app.query_one("#setup-save", Button).disabled
            assert await pilot.click("#setup-review-refresh")
            assert app.query_one("#setup-save", Button).disabled
            # A direct activation must also fail closed; disabled styling is not authority.
            await app.on_button_pressed(Button.Pressed(app.query_one("#setup-save", Button)))
            assert app._review_revision is None
            assert app.query_one("#setup-save", Button).disabled
    asyncio.run(exercise())
