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
from textual.widgets import Button, Checkbox, Collapsible, DataTable, Footer, Header, Input, Label, Select, Static, TabbedContent, TabPane, TextArea

from .cli_metadata import PRODUCTION_COMMANDS
from .configuration_workspace import ConfigurationWorkspace
from .dashboard_operations import EngineProgress, OperationPanel
from .models import Configuration, ConfigurationError
from .service import BackupService, ServiceError
from .progress import format_progress, observe_progress
from .production_operations import _operation_lease, execute_operation
from .service_status import operational_status


class BackupDashboard(App[None]):
    """Read durable state and run guarded operations; scheduled jobs use systemd."""
    TITLE = "bbackup"
    SUB_TITLE = "Encrypted backups • verified file restores"
    CSS = """
    Screen { background: $surface; }
    TabbedContent { height: 1fr; min-height: 5; }
    TabPane { height: 1fr; min-height: 3; padding: 1 2 3 2; overflow-y: auto; }
    #restore > VerticalScroll { height: 1fr; min-height: 1; }
    .intro { height: auto; margin-bottom: 1; }
    .toolbar { height: 3; margin-bottom: 1; }
    .toolbar Button { margin-right: 1; }
    DataTable { height: 1fr; min-height: 5; }
    #notice { dock: bottom; height: auto; padding: 1 2; background: $panel; }
    Input { margin-bottom: 1; }
    .configuration-editor { height: 1fr; min-height: 10; margin-bottom: 1; }
    """
    BINDINGS = [Binding("q", "quit", "Close"), Binding("r", "refresh", "Refresh"),
                Binding("s", "start", "Start job"), Binding("x", "stop", "Cancel job"),
                Binding("f1", "help", "Field help", priority=True),
                Binding("question_mark", "help", show=False, priority=True)]

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

    def _fit_toolbars(self, width: int) -> None:
        if not self.is_mounted:
            return
        layout = "vertical" if width < 72 else "horizontal"
        height = "auto" if width < 72 else 3
        for toolbar in self.query(".toolbar"):
            toolbar.styles.layout = layout
            toolbar.styles.height = height

    def on_resize(self, event):
        self._fit_toolbars(event.size.width)

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(initial="overview"):
            with TabPane("Overview", id="overview"):
                yield Static("Recovery status", classes="intro")
                yield Static("Start here: initialize each repository, run a backup, then verify a restore. A second copy is useful only when you can recover it independently.", classes="intro")
                with Horizontal(classes="toolbar"):
                    yield Button("Set up storage", id="next-storage", tooltip="Open repository initialization with your configured local storage selected.")
                    yield Button("Run first backup", id="next-backup", tooltip="Open your first configured job. Review it, then run once.")
                    yield Button("Verify a restore", id="next-restore", tooltip="Choose a successful snapshot, then restore into a new folder.")
                yield Static(id="overview-data", markup=False)
                yield Button("Refresh status", id="refresh", variant="primary")
            with TabPane("Jobs", id="jobs"):
                yield Static("Select a job to run it here. Install its schedule below for unattended systemd backups.", classes="intro")
                yield Input(placeholder="Search jobs", id="job-search")
                with Horizontal(classes="toolbar"):
                    yield Button("Start selected", id="start", variant="primary", disabled=True)
                    yield Button("Cancel selected", id="stop", variant="warning", disabled=True)
                yield DataTable(id="job-table", cursor_type="row")
                yield OperationPanel(("jobs run", "service render", "service install"), lambda: self.service,
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
                    yield Label("Repository")
                    yield Input(placeholder="Choose from Repositories", id="restore-repository", tooltip="Use a configured repository name. Select a snapshot to fill this automatically.")
                    yield Label("Full snapshot ID")
                    yield Input(placeholder="Select a snapshot in Snapshots", id="restore-snapshot", tooltip="Use the complete 64-character snapshot ID. Selecting a snapshot fills this for you.")
                    yield Label("New destination folder")
                    yield Input(placeholder="/absolute/path/to/new-restore", id="restore-target", tooltip="A new absolute path outside sources, repositories, password and state locations. Existing destinations are refused.")
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
                yield Static("Adjust a job here. Source and repository policy lives under Advanced. "
                             "Saving validates the complete policy, keeps a private backup, and refuses stale edits. "
                             "Schedule changes require reinstalling the timer.", classes="intro")
                yield Label("Job to edit")
                yield Select([(job.name, job.name) for job in self.service.config.jobs],
                             allow_blank=False, id="settings-job", tooltip="Choose the job whose schedule and time limit you want to change.")
                yield Label("Schedule")
                yield Input(id="settings-schedule", tooltip="A systemd calendar expression, for example daily or *-*-* 02:00:00. Saving does not activate a timer.")
                yield Label("Time limit per capture or copy stage (seconds)")
                yield Input(id="settings-timeout", type="integer", tooltip="Positive seconds. An interrupted operation is retained for review; it is never silently retried.")
                with Collapsible(title="Advanced: complete portable policy", collapsed=True):
                    yield Checkbox("Use advanced JSON instead of the job fields", id="settings-use-json")
                    yield TextArea("", id="settings-document", classes="configuration-editor",
                                   read_only=self.workspace is None)
                with Horizontal(classes="toolbar"):
                    yield Button("Reload configuration", id="settings-reload", disabled=self.workspace is None)
                    yield Button("Validate and save", id="settings-save", variant="primary",
                                 disabled=self.workspace is None)
                yield Static("", id="settings-status", markup=False)
        yield Static("Ready. Closing cancels interactive work; installed systemd timers continue.", id="notice", markup=False)
        yield Footer()

    def on_mount(self):
        self._fit_toolbars(self.size.width)
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
        observations = [
            ("Last local capture", facts["last_local_capture"]),
            ("Last additional copy", facts["last_independent_copy"]),
            ("Last full repository check", facts["last_repository_check"]),
            ("Last verified file restore", facts["last_verified_file_restore"]),
        ]
        kinds = {source.kind for source in self.service.config.sources}
        if kinds & {"mysql", "mariadb", "postgres", "sqlite"}:
            observations.append(("Last native database restore", facts["last_native_database_restore"]))
        if "docker" in kinds:
            observations.append(("Last container restore", facts["last_container_restore"]))
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
                "Next: run a backup and verify restored files." if not facts["last_verified_file_restore"] else "Keep testing restores after changes to your applications.",
            )
        )
        self.query_one("#overview-data", Static).update("\n".join(lines))

    def _refresh_activity(self):
        if not self.query("#event-table"):
            return
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
        if event.button.id == "next-storage":
            self.query_one(TabbedContent).active = "repositories"
            local = next((repo.name for repo in self.service.config.repositories if repo.role == "local"), None)
            if local:
                self.query_one("#repository-operations", OperationPanel).prepare("repositories init", {"name": local})
            self.query_one("#notice", Static).update("Initialize the local repository first. For each additional copy, initialize it with the local repository as its source.")
        elif event.button.id == "next-backup":
            self.query_one(TabbedContent).active = "jobs"
            if self.service.config.jobs:
                self.query_one("#job-operations", OperationPanel).prepare("jobs run", {"name": self.service.config.jobs[0].name})
            self.query_one("#notice", Static).update("Your job is selected below. Initialize its repositories first, then run it once.")
        elif event.button.id == "next-restore":
            self.query_one(TabbedContent).active = "snapshots"
            self.query_one("#notice", Static).update("Select a successful snapshot, open Restore, and choose a new absolute destination folder.")
        elif event.button.id == "refresh":
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
        if not Path(target).is_absolute() or Path(target).exists() or Path(target).is_symlink():
            status.update("Choose a new absolute destination folder. Existing paths are refused; your inputs have been kept.")
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

    def _restore_operation(self, repository, snapshot, target):
        if self.workspace is None:
            raise ConfigurationError("Open the dashboard through the configured CLI to authorize a restore.")
        return execute_operation("restore", {"repository": repository, "snapshot": snapshot, "target": str(target)},
                                 config_path=self.workspace.config_path, bindings_path=self.workspace.bindings_path,
                                 cancel=self._restore_cancel)

    def action_help(self):
        focused = self.focused
        self.query_one("#notice", Static).update(str(getattr(focused, "tooltip", None) or
            "Tab moves between controls; Enter selects a row or activates a button. Select a job, repository or snapshot to prepare its next action. Restore always uses a new folder."))

    def on_descendant_focus(self, event):
        if not self.is_mounted:
            return
        self.call_after_refresh(self._reveal_focused_control, event.widget)
        if event.widget.tooltip:
            self.query_one("#notice", Static).update(str(event.widget.tooltip))

    def _reveal_focused_control(self, widget):
        if not widget.is_mounted or not widget.region.height or not widget.region.width:
            return
        operation_panel = next(
            (ancestor for ancestor in widget.ancestors if isinstance(ancestor, OperationPanel)),
            None,
        )
        if operation_panel is not None:
            scroll_region = operation_panel.scrollable_content_region
            target_scroll = operation_panel.scroll_y
            if widget.region.bottom > scroll_region.bottom:
                target_scroll += widget.region.bottom - scroll_region.bottom
            elif widget.region.y < scroll_region.y:
                target_scroll -= scroll_region.y - widget.region.y
            if target_scroll != operation_panel.scroll_y:
                operation_panel.scroll_to(
                    y=target_scroll, animate=False, immediate=True, force=True
                )
            pane = next((ancestor for ancestor in operation_panel.ancestors
                         if isinstance(ancestor, TabPane)), None)
            if pane is not None:
                pane.scroll_to_widget(operation_panel, top=True, animate=False, immediate=True)
        visible = widget.region.intersection(self.screen.region)
        if (visible.height != widget.region.height or visible.width != widget.region.width):
            self.screen.scroll_to_widget(widget, top=True, animate=False, immediate=True)

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
                    self._restore_operation, repository, snapshot, target
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
            policy = Configuration.parse(json.loads(current["document"]))
            choices = self.query_one("#settings-job", Select)
            selected = choices.value
            choices.set_options([(job.name, job.name) for job in policy.jobs])
            if any(job.name == selected for job in policy.jobs):
                choices.value = selected
            self.query_one("#settings-use-json", Checkbox).value = False
            self._load_job_settings()
        except (ConfigurationError, OSError, UnicodeError):
            self._settings_revision = None
            status.update("Cannot load configuration. Check the configured path and permissions.")
        else:
            status.update("Loaded. Saving never updates credentials or enables system services.")

    def _load_job_settings(self):
        if not self.is_mounted:
            return
        name = self.query_one("#settings-job", Select).value
        try:
            policy = Configuration.parse(json.loads(self.query_one("#settings-document", TextArea).text))
        except (ValueError, ConfigurationError):
            return
        job = next((job for job in policy.jobs if job.name == name), None)
        if job is not None:
            self.query_one("#settings-schedule", Input).value = job.schedule
            self.query_one("#settings-timeout", Input).value = str(job.timeout_seconds)

    def on_select_changed(self, event: Select.Changed):
        if event.select.id == "settings-job":
            self._load_job_settings()

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
        if not self.query_one("#settings-use-json", Checkbox).value:
            try:
                policy = json.loads(document)
                name = self.query_one("#settings-job", Select).value
                job = next(job for job in policy["jobs"] if job["name"] == name)
                job["schedule"] = self.query_one("#settings-schedule", Input).value.strip()
                job["timeout_seconds"] = int(self.query_one("#settings-timeout", Input).value)
                document = json.dumps(policy, indent=2) + "\n"
            except (ValueError, StopIteration, KeyError, TypeError):
                status.update("Choose a job, a calendar schedule, and a positive whole-number time limit. Your input has been kept.")
                return
        self.query_one("#settings-save", Button).disabled = True
        try:
            await asyncio.to_thread(self.workspace.save, document, self._settings_revision)
            with _operation_lease(self.workspace.config_path, self.workspace.bindings_path) as service:
                self.service = service
        except (ConfigurationError, ServiceError, OSError, UnicodeError) as error:
            status.update(f"Save did not complete: {error} Your unsaved text has been kept. Reload only if you want to discard it.")
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
        panel = self.query_one("#job-operations", OperationPanel)
        notice = self.query_one("#notice", Static)
        if action == "start":
            if panel.busy:
                notice.update("A job operation is already running. Review its progress below.")
                return
            panel.prepare("jobs run", {"name": self.selected_job})
            panel.start()
        elif (panel.busy and panel._command == "jobs run"
              and panel._running_values.get("name") == self.selected_job):
            await panel.cancel_and_wait()
        else:
            notice.update("No interactive run of this job is active. Installed timers are managed separately.")



# Preserve the public dashboard import while keeping guided setup focused.
from .dashboard_setup import BackupSetup as BackupSetup  # noqa: E402
