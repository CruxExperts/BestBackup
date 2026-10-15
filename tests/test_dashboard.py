# ruff: noqa: F811
import asyncio
import shutil

import pytest

from textual.widgets import Button, DataTable, Input

from bbackup.dashboard import BackupDashboard
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


@pytest.mark.skipif(shutil.which("restic") is None, reason="restic is required")
def test_dashboard_restore_verifies_files_and_refuses_existing_target(service, tmp_path):
    from textual.widgets import Static, TabbedContent

    service.initialize("local")
    snapshot = service.capture("daily")["snapshot_id"]

    async def exercise():
        app = BackupDashboard(service)
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
            assert "did not complete" in str(app.query_one("#restore-status", Static).render())
            assert app.query_one("#restore-cancel", Button).disabled
            assert not app.query_one("#restore-start", Button).disabled

    asyncio.run(exercise())
