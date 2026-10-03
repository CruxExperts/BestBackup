"""Mouse and keyboard dashboard over the shared production service."""
from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Header, Input, Static, TabbedContent, TabPane

from .service import BackupService


class BackupDashboard(App):
    """Read durable state; supervise jobs independently of the terminal session."""
    TITLE = "bbackup"
    SUB_TITLE = "Local recovery • independent copies"
    CSS = """
    Screen { background: $surface; }
    TabPane { padding: 1 2; }
    .intro { height: auto; margin-bottom: 1; }
    .toolbar { height: 3; margin-bottom: 1; }
    .toolbar Button { margin-right: 1; }
    DataTable { height: 1fr; min-height: 5; }
    #notice { dock: bottom; height: auto; padding: 1 2; background: $panel; }
    Input { margin-bottom: 1; }
    """
    BINDINGS = [Binding("q", "quit", "Detach"), Binding("r", "refresh", "Refresh"),
                Binding("s", "start", "Start job"), Binding("x", "stop", "Cancel job")]

    def __init__(self, service: BackupService):
        super().__init__()
        self.service = service
        self.selected_job = None
        self._sort_reverse = False

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                yield Static("Recovery status", classes="intro")
                yield Static(id="overview-data")
                yield Button("Refresh status", id="refresh", variant="primary")
            with TabPane("Jobs", id="jobs"):
                yield Static("Select a job, then start or cancel its installed systemd service.", classes="intro")
                yield Input(placeholder="Search jobs", id="job-search")
                with Horizontal(classes="toolbar"):
                    yield Button("Start selected", id="start", variant="primary", disabled=True)
                    yield Button("Cancel selected", id="stop", variant="warning", disabled=True)
                yield DataTable(id="job-table", cursor_type="row")
            with TabPane("Repositories", id="repositories"):
                yield Static("Repository names and roles. Credentials stay outside the dashboard.", classes="intro")
                yield DataTable(id="repository-table", cursor_type="row")
            with TabPane("Snapshots", id="snapshots"):
                yield Static("Successful local captures. A snapshot receipt alone is not a verified recovery drill.", classes="intro")
                yield DataTable(id="snapshot-table", cursor_type="row")
            with TabPane("Recovery", id="recovery"):
                with VerticalScroll():
                    yield Static("Cloud compromise protection: NOT QUALIFIED\n\n"
                                 "No independent recovery checkpoint is configured. Repository checks verify stored data; "
                                 "application recovery requires a restore drill.\n\n"
                                 "Use the restore command with a complete snapshot ID and a new directory. "
                                 "Existing targets, source paths, repositories and runtime state are refused.")
            with TabPane("Activity", id="activity"):
                yield Static("Latest page of sanitized lifecycle events. Commands, credentials and raw output are omitted.", classes="intro")
                yield DataTable(id="event-table", cursor_type="row")
            with TabPane("Settings", id="settings"):
                yield Static("Daily schedule by default • persistent missed runs\n"
                             "Local: seven daily snapshots\nCloud policy: thirty daily, eight weekly, twelve monthly\n\n"
                             "Retention deletion and cloud protection are not enabled in this preview.\n"
                             "Edit the portable configuration and private host bindings, then reopen the dashboard.")
        yield Static("Ready. Closing this dashboard detaches from systemd jobs.", id="notice", markup=False)
        yield Footer()

    def on_mount(self):
        columns = {"job-table": ("Job", "Local repository", "Replicas", "Schedule"),
                   "repository-table": ("Repository", "Role"),
                   "snapshot-table": ("Repository", "Snapshot ID", "Job"),
                   "event-table": ("When", "State", "Operation ID", "Exit")}
        for widget_id, headings in columns.items():
            self.query_one("#" + widget_id, DataTable).add_columns(*headings)
        self.action_refresh()

    def action_refresh(self):
        self._jobs(self.query_one("#job-search", Input).value)
        repositories = self.query_one("#repository-table", DataTable)
        repositories.clear()
        for repo in self.service.config.repositories:
            repositories.add_row(repo.name, repo.role)
        snapshots = self.service.snapshots(limit=1000)
        table = self.query_one("#snapshot-table", DataTable)
        table.clear()
        for row in snapshots:
            table.add_row(row["repository"], row["snapshot_id"], row["job"])
        events = self.service.ledger.events(limit=1000, newest_first=True)
        table = self.query_one("#event-table", DataTable)
        table.clear()
        for event in events:
            table.add_row(event.occurred_at, str(event.kind), event.operation_id,
                          str(event.exit_code) if event.exit_code is not None else "—")
        self.query_one("#overview-data", Static).update(
            f"Configured jobs: {len(self.service.config.jobs)}\n"
            f"Local snapshot receipts shown: {len(snapshots)} (maximum 1000)\n"
            "Cloud protection expiry: unavailable\nVerified application recovery: not recorded\n\n"
            "Production qualification remains incomplete.\n")

    def _jobs(self, query):
        table = self.query_one("#job-table", DataTable)
        table.clear()
        for job in self.service.config.jobs:
            if query.casefold() in job.name.casefold():
                table.add_row(job.name, job.repository, ", ".join(job.replicas) or "None", job.schedule, key=job.name)
        self.selected_job = None
        self.query_one("#start", Button).disabled = True
        self.query_one("#stop", Button).disabled = True

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "job-search" and self.is_mounted:
            self._jobs(event.value)

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        if event.data_table.id == "job-table":
            self.selected_job = str(event.row_key.value)
            self.query_one("#start", Button).disabled = False
            self.query_one("#stop", Button).disabled = False

    def on_data_table_header_selected(self, event: DataTable.HeaderSelected):
        self._sort_reverse = not self._sort_reverse
        event.data_table.sort(event.column_key, reverse=self._sort_reverse)

    async def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "refresh":
            self.action_refresh()
        elif event.button.id == "start":
            await self.action_start()
        elif event.button.id == "stop":
            await self.action_stop()

    async def action_start(self):
        await self._supervise("start")

    async def action_stop(self):
        await self._supervise("stop")

    async def _supervise(self, action):
        if self.selected_job is None:
            self.query_one("#notice", Static).update("Select a job first.")
            return
        unit = "bbackup-" + self.selected_job + ".service"
        notice = self.query_one("#notice", Static)
        try:
            child = await asyncio.create_subprocess_exec(
                "systemctl", "--no-ask-password", "--no-block", action, unit,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            try:
                code = await asyncio.wait_for(child.wait(), timeout=10)
            except asyncio.TimeoutError:
                child.kill()
                await child.wait()
                notice.update("Service response timed out. Reconcile its status before retrying.")
                return
        except OSError:
            notice.update("Systemd is unavailable. Install the rendered service before running jobs here.")
            return
        if code:
            notice.update("Service request failed. Check installation and operator permissions.")
        else:
            notice.update(f"Service {action} requested for {self.selected_job}. Refresh activity to verify the outcome.")
