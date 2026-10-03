# ruff: noqa: F811
import asyncio

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
