"""Mouse and keyboard dashboard over the shared production service."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sqlite3
from threading import Event

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, DataTable, Footer, Header, Input, Static, TabbedContent, TabPane, TextArea

from .cli_metadata import PRODUCTION_COMMANDS
from .configuration_workspace import ConfigurationWorkspace
from .dashboard_operations import EngineProgress, OperationPanel
from .models import Configuration, ConfigurationError, HostBindings
from .service import BackupService, ServiceError
from .progress import format_progress, observe_progress
from .service_status import operational_status


class BackupDashboard(App[None]):
    """Read durable state; supervise jobs independently of the terminal session."""
    TITLE = "bbackup"
    SUB_TITLE = "Local recovery • independent copies"
    CSS = """
    Screen { background: $surface; }
    TabPane { padding: 1 2; overflow-y: auto; }
    .intro { height: auto; margin-bottom: 1; }
    .toolbar { height: 3; margin-bottom: 1; }
    .toolbar Button { margin-right: 1; }
    DataTable { height: 1fr; min-height: 5; }
    #notice { dock: bottom; height: auto; padding: 1 2; background: $panel; }
    Input { margin-bottom: 1; }
    .configuration-editor { height: 1fr; min-height: 10; margin-bottom: 1; }
    """
    BINDINGS = [Binding("q", "quit", "Close"), Binding("r", "refresh", "Refresh"),
                Binding("s", "start", "Start job"), Binding("x", "stop", "Cancel job")]

    def __init__(self, service: BackupService, *, workspace: ConfigurationWorkspace | None = None):
        super().__init__()
        self.service = service
        self.workspace = workspace
        self._settings_revision = None
        self.selected_job = None
        self._sort_reverse = False
        self._restore_cancel = Event()
        self._restore_worker = None
        self._restoring = False

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                yield Static("Recovery status", classes="intro")
                yield Static(id="overview-data", markup=False)
                yield Button("Refresh status", id="refresh", variant="primary")
            with TabPane("Jobs", id="jobs"):
                yield Static("Select a job, then start or cancel its installed systemd service.", classes="intro")
                yield Input(placeholder="Search jobs", id="job-search")
                with Horizontal(classes="toolbar"):
                    yield Button("Start selected", id="start", variant="primary", disabled=True)
                    yield Button("Cancel selected", id="stop", variant="warning", disabled=True)
                yield DataTable(id="job-table", cursor_type="row")
                yield OperationPanel(("jobs run", "service render"), lambda: self.service,
                                     self.workspace, self.action_refresh, id="job-operations")
            with TabPane("Repositories", id="repositories"):
                yield Static("Repository names and roles. Credentials stay outside the dashboard.", classes="intro")
                yield DataTable(id="repository-table", cursor_type="row")
                yield OperationPanel(("repositories init", "repositories check"), lambda: self.service,
                                     self.workspace, self.action_refresh, id="repository-operations")
            with TabPane("Snapshots", id="snapshots"):
                yield Static("Successful local captures. A snapshot receipt alone is not a verified recovery drill.", classes="intro")
                yield DataTable(id="snapshot-table", cursor_type="row")
                yield OperationPanel(("snapshots copy", "retention plan", "retention apply"),
                                     lambda: self.service, self.workspace, self.action_refresh,
                                     id="snapshot-operations")
            with TabPane("Restore", id="restore"):
                with VerticalScroll():
                    yield Static(
                        "Restore verified files into a NEW directory. Existing data is never overwritten. "
                        "Select a snapshot in Snapshots or enter its full ID. "
                        "File integrity is checked; application recovery still needs your own checks.",
                        classes="intro",
                    )
                    yield Input(placeholder="Repository name", id="restore-repository")
                    yield Input(placeholder="Full snapshot ID", id="restore-snapshot")
                    yield Input(placeholder="Absolute new destination directory", id="restore-target")
                    with Horizontal(classes="toolbar"):
                        yield Button("Restore to new directory", id="restore-start", variant="primary")
                        yield Button("Cancel restore", id="restore-cancel", disabled=True)
                    yield Static("", id="restore-status", markup=False)
            with TabPane("Recovery", id="recovery"):
                yield Static(
                    "Cloud compromise protection remains NOT QUALIFIED. "
                    "Independent kits must be kept outside the protected host. "
                    "Inspect exact inputs; trust and quiescence acknowledgements are required where applicable.",
                    classes="intro",
                )
                yield OperationPanel(tuple(name for name in PRODUCTION_COMMANDS
                                           if name.startswith(("recovery ", "databases ", "docker "))
                                           or name == "runs reconcile"),
                                     lambda: self.service, self.workspace, self.action_refresh,
                                     id="recovery-operations")
            with TabPane("Activity", id="activity"):
                yield Static("Latest page of sanitized lifecycle events. Commands, credentials and raw output are omitted.", classes="intro")
                yield DataTable(id="event-table", cursor_type="row")
            with TabPane("Settings", id="settings"):
                yield Static(
                    "Edit the complete portable JSON configuration. Private credentials remain in host bindings. "
                    "Saving validates all fields and bindings, keeps a private compressed backup, "
                    "and refuses stale revisions. Schedule changes require regenerating installed timers.",
                    classes="intro",
                )
                yield TextArea("", id="settings-document", classes="configuration-editor",
                               read_only=self.workspace is None)
                with Horizontal(classes="toolbar"):
                    yield Button("Reload configuration", id="settings-reload", disabled=self.workspace is None)
                    yield Button("Validate and save", id="settings-save", variant="primary",
                                 disabled=self.workspace is None)
                yield Static("", id="settings-status", markup=False)
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
        self._load_settings()
        self.set_interval(1, self._refresh_activity)

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
        self._refresh_activity()
        self.run_worker(self._refresh_overview(), group="overview", exclusive=True)

    async def _refresh_overview(self) -> None:
        try:
            facts = await asyncio.to_thread(operational_status, self.service)
        except (OSError, sqlite3.Error):
            self.query_one("#overview-data", Static).update("Status unavailable; inspect local state.")
            return
        lines = [f"Configured jobs: {len(self.service.config.jobs)}"]
        observations = (
            ("Last local capture", facts["last_local_capture"]),
            ("Last independent copy", facts["last_independent_copy"]),
            ("Last cloud copy", facts["last_cloud_copy"]),
            ("Last full repository check", facts["last_repository_check"]),
            ("Last verified file restore", facts["last_verified_file_restore"]),
            ("Last native database restore", facts["last_native_database_restore"]),
            ("Last container restore", facts["last_container_restore"]),
        )
        lines.extend(f"{label}: {observed or 'not recorded'}" for label, observed in observations)

        counts = facts["operation_counts"]
        lines.append(
            "Operation outcomes: "
            + (
                ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))
                or "none recorded"
            )
        )
        for repository in facts["repositories"]:
            free_bytes = repository["free_bytes"]
            total_bytes = repository["total_bytes"]
            if free_bytes is None or total_bytes is None:
                capacity = repository["capacity_source"]
            else:
                capacity = (
                    f"{free_bytes / 1024**3:.2f} GiB free of "
                    f"{total_bytes / 1024**3:.2f} GiB"
                )
                if repository["capacity_source"] != "repository filesystem":
                    capacity += f" ({repository['capacity_source']})"
            lines.append(
                f"Repository {repository['name']} ({repository['role']}): {capacity}"
            )

        for schedule in facts["schedules"]:
            next_run = schedule["next_run"] or "no observed active timer"
            active_timer = schedule["active_timer"] or "none observed"
            lines.append(
                f"Job {schedule['name']}: next {next_run}; active timer "
                f"{active_timer}; configured {schedule['configured']}"
            )

        checkpoint = facts["last_checkpoint"]
        if checkpoint is None:
            lines.append(
                "Recovery checkpoint for current repository bindings: not recorded"
            )
        else:
            expires_at = checkpoint["expires_at"] or "no expiry recorded"
            lines.append(
                f"Latest recovery checkpoint for {checkpoint['repository']}: "
                f"expires {expires_at}; state {checkpoint['expiry_state']}"
            )
        lines.extend(
            (
                f"Application recovery: {facts['application_recovery']}",
                facts["protection_status"],
                "Production qualification remains incomplete.",
            )
        )
        self.query_one("#overview-data", Static).update("\n".join(lines))

    def _refresh_activity(self):
        events = self.service.ledger.events(limit=1000, newest_first=True)
        table = self.query_one("#event-table", DataTable)
        table.clear()
        for event in events:
            table.add_row(event.occurred_at, str(event.kind), event.operation_id,
                          str(event.exit_code) if event.exit_code is not None else "—")

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
            self.query_one("#job-operations", OperationPanel).prepare("jobs run", {"name": self.selected_job})

        elif event.data_table.id == "repository-table":
            repository, _ = event.data_table.get_row(event.row_key)
            self.query_one("#repository-operations", OperationPanel).prepare(
                "repositories check", {"name": str(repository)}
            )
        elif event.data_table.id == "snapshot-table":
            repository, snapshot, _ = event.data_table.get_row(event.row_key)
            self.query_one("#restore-repository", Input).value = str(repository)
            self.query_one("#restore-snapshot", Input).value = str(snapshot)
            self.query_one("#snapshot-operations", OperationPanel).prepare(
                "snapshots copy", {"source": str(repository), "destination": "", "snapshot": str(snapshot)}
            )
            self.query_one("#notice", Static).update(
                "Snapshot selected. Open Restore and choose a new destination directory."
            )
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
        elif event.button.id == "restore-start":
            self._start_restore()
        elif event.button.id == "restore-cancel":
            self._restore_cancel.set()
            self.query_one("#restore-status", Static).update(
                "Cancellation requested. Partial output will be retained for inspection."
            )
        elif event.button.id == "settings-reload":
            self._load_settings()
        elif event.button.id == "settings-save":
            await self._save_settings()

    def _start_restore(self):
        if self._restore_worker is not None and not self._restore_worker.is_finished:
            return
        repository = self.query_one("#restore-repository", Input).value
        snapshot = self.query_one("#restore-snapshot", Input).value
        target = self.query_one("#restore-target", Input).value
        status = self.query_one("#restore-status", Static)
        if not repository or not snapshot or not target:
            status.update("Repository, full snapshot ID, and new destination are required.")
            return
        self._restore_cancel = Event()
        self._restoring = True
        self.query_one("#restore-start", Button).disabled = True
        self.query_one("#restore-cancel", Button).disabled = False
        status.update("Restoring files and verifying integrity. Do not reuse the destination.")
        self._restore_worker = self.run_worker(
            self._restore(repository, snapshot, Path(target)),
            name="restore", group="restore", exit_on_error=False,
        )

    def _report_restore_progress(self, data):
        self.post_message(EngineProgress(data))

    def on_engine_progress(self, message: EngineProgress):
        if self._restoring:
            self.query_one("#restore-status", Static).update(
                f"Restoring and verifying: {format_progress(message.data)}. Partial output is retained on cancellation."
            )

    async def _restore(self, repository, snapshot, target):
        try:
            with observe_progress(self._report_restore_progress):
                await asyncio.to_thread(
                    self.service.restore, repository, snapshot, target, cancel=self._restore_cancel
                )
        except Exception:
            self.query_one("#restore-status", Static).update(
                "Restore did not complete. Check the destination and activity before retrying; "
                "partial files are retained. Credentials and raw command output are omitted."
            )
        else:
            self.query_one("#restore-status", Static).update(
                "Restored files verified. Application startup and data correctness are not validated."
            )
        finally:
            self._restoring = False
            self.query_one("#restore-start", Button).disabled = False
            self.query_one("#restore-cancel", Button).disabled = True
            self.action_refresh()

    def _load_settings(self):
        status = self.query_one("#settings-status", Static)
        if self.workspace is None:
            status.update("Configuration editing requires the configured CLI dashboard entry point.")
            return
        try:
            current = self.workspace.read()
            self.query_one("#settings-document", TextArea).load_text(current["document"])
            self._settings_revision = current["revision"]
        except (ConfigurationError, OSError, UnicodeError):
            self._settings_revision = None
            status.update("Cannot load configuration. Check the configured path and permissions.")
        else:
            status.update("Loaded. Saving never updates credentials or enables system services.")

    async def _save_settings(self):
        status = self.query_one("#settings-status", Static)
        if self.workspace is None or self._settings_revision is None:
            status.update("Reload a valid configuration before saving.")
            return
        if ((self._restore_worker is not None and not self._restore_worker.is_finished)
                or any(panel.busy for panel in self.query(OperationPanel))):
            status.update("Wait for interactive operations to finish before changing configuration.")
            return
        document = self.query_one("#settings-document", TextArea).text
        self.query_one("#settings-save", Button).disabled = True
        try:
            await asyncio.to_thread(self.workspace.save, document, self._settings_revision)
            self.service = BackupService(Configuration.load(self.workspace.config_path),
                                         HostBindings.load(self.workspace.bindings_path))
        except (ConfigurationError, ServiceError, OSError, UnicodeError):
            status.update("Save did not complete. Check JSON, binding compatibility, and permissions. "
                          "If another editor changed the file, reload before retrying.")
        else:
            self._load_settings()
            status.update("Saved with a private compressed backup. Regenerate installed timers if schedules changed.")
            self.action_refresh()
        finally:
            self.query_one("#settings-save", Button).disabled = False

    async def action_quit(self):
        waiting = [panel.cancel_and_wait() for panel in self.query(OperationPanel)]
        if self._restore_worker is not None and not self._restore_worker.is_finished:
            self._restore_cancel.set()
            waiting.append(self._restore_worker.wait())
        await asyncio.gather(*waiting)
        self.exit()

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


class BackupSetup(App[BackupService | None]):
    """Enroll explicit new config paths before opening the normal dashboard."""

    TITLE = "bbackup setup"
    CSS = """
    Screen { padding: 1 2; }
    TextArea { height: 1fr; min-height: 5; margin-bottom: 1; }
    Static { height: auto; }
    #setup-status { margin-top: 1; }
    """
    BINDINGS = [Binding("q", "quit", "Cancel setup")]

    def __init__(self, workspace: ConfigurationWorkspace):
        super().__init__()
        self.workspace = workspace
        self._saving = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(
            "Create NEW portable configuration and private host-binding files. "
            "Enter version-2 JSON using the same schema as the command line. "
            "Only credential file references belong here, never passwords or access tokens. "
            "Setup does not initialize repositories, capture files, or enable services."
        )
        yield Static(f"New configuration: {self.workspace.config_path}", markup=False)
        yield TextArea(json.dumps({"schema_version": 2, "repositories": [], "sources": [], "jobs": []},
                                  indent=2), id="setup-configuration")
        yield Static(f"New private bindings: {self.workspace.bindings_path}", markup=False)
        yield TextArea(json.dumps({"schema_version": 2,
                                   "state_dir": str(self.workspace.bindings_path.parent / "state"),
                                   "repositories": {}}, indent=2), id="setup-bindings")
        yield Button("Validate and create both files", id="setup-save", variant="primary")
        yield Static("", id="setup-status", markup=False)
        yield Footer()

    async def on_button_pressed(self, event: Button.Pressed):
        if event.button.id != "setup-save" or self._saving:
            return
        self._saving = True
        event.button.disabled = True
        status = self.query_one("#setup-status", Static)
        status.update("Validating and creating private files. Existing files will never be replaced.")
        try:
            service = await asyncio.to_thread(
                self.workspace.setup,
                self.query_one("#setup-configuration", TextArea).text,
                self.query_one("#setup-bindings", TextArea).text,
            )
        except (ConfigurationError, ServiceError, OSError, UnicodeError):
            status.update("Setup did not complete. Check JSON, matching bindings, new file paths, "
                          "and private-directory permissions. Existing files were not overwritten.")
        else:
            self.exit(service)
        finally:
            self._saving = False
            event.button.disabled = False

    async def action_quit(self):
        if self._saving:
            self.query_one("#setup-status", Static).update("Wait for configuration saving to finish before exiting.")
        else:
            self.exit()
