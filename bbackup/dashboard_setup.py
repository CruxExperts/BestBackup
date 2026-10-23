"""Guided local-first setup over the shared configuration workspace."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import secrets

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Button, Checkbox, Collapsible, ContentSwitcher, Footer, Header, Input, Label, Static, TextArea

from .configuration_workspace import ConfigurationWorkspace
from .models import Configuration, ConfigurationError
from .production_operations import _operation_lease
from .service import BackupService, ServiceError


class BackupSetup(App[BackupService | None]):
    """Collect ordinary settings with one review, keeping expert JSON available."""

    TITLE = "bbackup setup"
    SUB_TITLE = "Your first backup starts here"
    CSS = """
    Screen { padding: 0 2; }
    #setup-progress, #setup-status { height: auto; margin: 1 0; }
    ContentSwitcher { height: 1fr; min-height: 1; }
    ContentSwitcher VerticalScroll { height: 1fr; min-height: 1; padding: 0 1; }
    Label { margin-top: 1; }
    .setup-help { height: auto; color: $text-muted; margin-bottom: 1; }
    #setup-review { height: auto; }
    #setup-navigation { height: 3; margin-top: 1; }
    Button { margin-right: 1; }
    TextArea { height: 12; }
    """
    BINDINGS = [Binding("q", "quit", "Cancel setup"),
                Binding("f1", "help", "Field help", priority=True),
                Binding("question_mark", "help", show=False, priority=True)]
    STEPS = ("Sources", "Encrypted storage", "Schedule", "Review and enroll")

    def __init__(self, workspace: ConfigurationWorkspace):
        super().__init__()
        self.workspace = workspace
        self._step = 0
        self._saving = False
        self._advanced_loaded = False
        self._existing = False
        self._existing_version = None
        self._review_revision = None

    def compose(self) -> ComposeResult:
        home = Path.home()
        yield Header()
        yield Static("Step 1 of 4 · Sources", id="setup-progress", markup=False)
        with ContentSwitcher(initial="setup-step-0"):
            with VerticalScroll(id="setup-step-0"):
                yield Static("What would you like to protect?", classes="intro")
                yield Label("Source folder")
                yield Input(placeholder="/srv/documents", id="setup-source", tooltip="An existing absolute folder path. Hidden files are included; nothing is excluded silently.")
                yield Static("Choose one existing folder. Hidden files are included. Add advanced source types later in Settings.", classes="setup-help")
                yield Label("Backup job name")
                yield Input("daily", id="setup-job", tooltip="A short unique name, such as daily. Used in Activity and the timer name.")
            with VerticalScroll(id="setup-step-1"):
                yield Label("Encrypted local repository")
                yield Input(str(home / "backup-local"), id="setup-repository", tooltip="An absolute storage path outside your source, password and state folders. An empty repository will be initialized separately.")
                yield Label("Local repository password file")
                yield Input(str(home / ".config/bbackup/local.password"), id="setup-password", tooltip="Use an existing owner-only password file, or choose a new path and bbackup will generate it after review. Never paste a password.")
                yield Static("New password files are generated privately after review. Keep an independent recovery copy: losing the password means losing access.", classes="setup-help")
                yield Label("Additional local copy (optional)")
                yield Input(placeholder="/mnt/another-disk/backups", id="setup-replica", tooltip="Optional second repository. A second directory on this host does not prove protection against loss of the host.")
                yield Label("Additional-copy password file")
                yield Input(str(home / ".config/bbackup/replica.password"), id="setup-replica-password")
                yield Static("Use separate storage for a useful additional copy. Cloud providers remain unqualified and are available through Advanced configuration.", classes="setup-help")
            with VerticalScroll(id="setup-step-2"):
                yield Label("Schedule")
                yield Input("daily", id="setup-schedule", tooltip="A systemd calendar expression, for example daily, weekly, or *-*-* 02:00:00. The schedule stays disabled until you explicitly apply it.")
                yield Static("The schedule is saved with your policy. Setup does not silently install or activate a timer. You can render and apply it after verifying the first restore.", classes="setup-help")
                yield Label("Maximum seconds for each capture or copy")
                yield Input("3600", type="integer", id="setup-timeout", tooltip="Allow enough time for your data size. An interrupted mutation requires inspection before another attempt.")
            with VerticalScroll(id="setup-step-3"):
                yield Static("", id="setup-review", markup=False)
                with Collapsible(title="Advanced: edit JSON", collapsed=True):
                    yield Checkbox("Use these JSON documents instead of the guided fields", id="setup-use-json")
                    yield Label("Portable policy")
                    yield TextArea("", id="setup-configuration")
                    yield Label("Private host bindings (file references only)")
                    yield TextArea("", id="setup-bindings")
                yield Button("Refresh mappings", id="setup-review-refresh")
                yield Button("Create private files and enroll", id="setup-save", variant="primary")
        yield Static("Choose the folder you want to protect, then continue.", id="setup-status", markup=False)
        with Horizontal(id="setup-navigation"):
            yield Button("Back", id="setup-back", disabled=True)
            yield Button("Continue", id="setup-next", variant="primary")
        yield Footer()

    def on_mount(self):
        self.query_one("#setup-review-refresh").display = False
        if self.workspace.config_path.exists() or self.workspace.bindings_path.exists():
            self._existing = True
            self._review_revision = None
            self.query_one("#setup-review-refresh").display = True
            self._navigate(3)
            self.query_one("#setup-navigation").display = False
            self.query_one(Collapsible).display = False
            try:
                candidates = self.workspace.candidates()
                self._existing_version = candidates["schema_version"]
                self._review_revision = candidates["review_revision"]
                action = "Migrate and enroll" if self._existing_version == 2 else "Enroll reviewed configuration"
                self.query_one("#setup-save", Button).label = action
                self.query_one("#setup-review", Static).update(
                    f"{action} on this machine and user\n\n"
                    f"Policy: {self.workspace.config_path}\nPrivate bindings: {self.workspace.bindings_path}\n"
                    f"Jobs: {', '.join(candidates['jobs'])}\n" +
                    "\n".join(f"Source {item['name']} ({item['kind']}): {', '.join(item['selector'])}" for item in candidates["sources"]) +
                    "\n" + "\n".join(f"Repository {item['name']}: {item['location']}" for item in candidates["repositories"]) +
                    "\n\nConfirm these mappings before continuing. "
                    "Migration writes new .v3 files and preserves originals. Enrollment authorizes these exact files here; services remain disabled."
                )
            except (ConfigurationError, OSError, UnicodeError) as error:
                self.query_one("#setup-review", Static).update(
                    f"Policy: {self.workspace.config_path}\nPrivate bindings: {self.workspace.bindings_path}\n"
                    "Correct these files, then refresh their mappings before enrollment.")
                self.query_one("#setup-status", Static).update(f"Cannot inspect the existing configuration: {error}. Correct the files before enrollment; nothing was changed.")
                self.query_one("#setup-save", Button).disabled = True

    def _value(self, name: str) -> str:
        return self.query_one(f"#setup-{name}", Input).value.strip()

    def _documents(self):
        source = Path(self._value("source"))
        if not source.is_absolute() or not source.is_dir():
            raise ConfigurationError("Source folder must be an existing absolute directory. Your input has been kept.")
        job = self._value("job")
        if not job:
            raise ConfigurationError("Give this job a short name, such as daily.")
        selectors = {"local": (self._value("repository"), self._value("password"))}
        if self._value("replica"):
            selectors["replica"] = (self._value("replica"), self._value("replica-password"))
        for repository, password in selectors.values():
            if not Path(repository).is_absolute() or not Path(password).is_absolute():
                raise ConfigurationError("Storage and password-file paths must be absolute.")
            for protected in (Path(repository), Path(password), self.workspace.bindings_path.parent / "state"):
                if source == protected or source in protected.parents or protected in source.parents:
                    raise ConfigurationError("Keep source, repository, password and state paths separate; choose a different storage location.")
        try:
            timeout = int(self._value("timeout"))
        except ValueError:
            raise ConfigurationError("Timeout must be a positive whole number of seconds.") from None
        if timeout <= 0:
            raise ConfigurationError("Timeout must be a positive whole number of seconds.")
        policy = {"schema_version": 3,
                  "repositories": [{"name": name, "role": "local" if name == "local" else "replica"} for name in selectors],
                  "sources": [{"name": "files", "kind": "files"}],
                  "jobs": [{"name": job, "repository": "local", "sources": ["files"],
                            "replicas": [name for name in selectors if name != "local"],
                            "schedule": self._value("schedule"), "timeout_seconds": timeout}]}
        bindings = {"schema_version": 3, "state_dir": str(self.workspace.bindings_path.parent / "state"),
                    "repositories": {name: {"repository": repository, "password_file": password}
                                     for name, (repository, password) in selectors.items()},
                    "source_bindings": {"files": {"paths": [str(source)]}}}
        Configuration.parse(policy)
        protected = [Path(value).resolve() for pair in selectors.values() for value in pair]
        if len(set(protected)) != len(protected):
            raise ConfigurationError("Use different paths for each repository and password file.")
        return policy, bindings

    def _show_review(self):
        policy, bindings = self._documents()
        self.query_one("#setup-review", Static).update(
            f"Protect: {self._value('source')}\n"
            f"Local encrypted storage: {self._value('repository')}\n"
            f"Additional copy: {self._value('replica') or 'Not configured'}\n"
            f"Schedule: {self._value('schedule')} (not activated)\n\n"
            f"Portable policy: {self.workspace.config_path}\n"
            f"Private bindings: {self.workspace.bindings_path}\n\n"
            "This enrolls these exact choices for this machine and user. New password files will be generated where needed. "
            "Existing files are never replaced. After setup, initialize storage, run your first backup, and verify a restore."
        )
        if not self._advanced_loaded:
            self.query_one("#setup-configuration", TextArea).load_text(json.dumps(policy, indent=2))
            self.query_one("#setup-bindings", TextArea).load_text(json.dumps(bindings, indent=2))
            self._advanced_loaded = True

    def _navigate(self, step: int):
        self._step = step
        self.query_one(ContentSwitcher).current = f"setup-step-{step}"
        self.query_one("#setup-progress", Static).update(f"Step {step + 1} of 4 · {self.STEPS[step]}")
        self.query_one("#setup-back", Button).disabled = step == 0
        self.query_one("#setup-next", Button).display = step < 3
        self.query_one("#setup-next", Button).remove_class("-active")
        self.query_one("#setup-status", Static).update("Your changes are kept when you move between steps.")

    def _create(self, policy, bindings):
        # Only guided new setup may generate passwords; advanced documents must reference existing files.
        created = []
        try:
            for repository in bindings["repositories"].values():
                password = Path(repository["password_file"])
                if password.exists() or password.is_symlink():
                    continue
                password.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                if password.parent.is_symlink() or password.parent.stat().st_uid != os.geteuid() or password.parent.stat().st_mode & 0o077:
                    raise ConfigurationError("Password directory must be private, owned by you, and not a symlink.")
                descriptor = os.open(password, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, "w") as output:
                    output.write(secrets.token_hex(32) + "\n")
                    output.flush()
                    os.fsync(output.fileno())
                created.append(password)
            return self.workspace.setup(json.dumps(policy), json.dumps(bindings))
        except Exception:
            # Preserve generated recovery material; explain the retry instead of silently deleting it.
            if created:
                raise ConfigurationError("Setup did not finish. Generated password files were retained privately; reuse them when correcting the input.") from None
            raise

    async def on_button_pressed(self, event: Button.Pressed):
        if self._saving:
            return
        status = self.query_one("#setup-status", Static)
        try:
            if event.button.id == "setup-review-refresh":
                self.on_mount()
            elif event.button.id == "setup-back":
                self._navigate(max(0, self._step - 1))
            elif event.button.id == "setup-next":
                if self._step == 0 and (not Path(self._value("source")).is_absolute() or not Path(self._value("source")).is_dir()):
                    raise ConfigurationError("Choose an existing absolute source folder before continuing.")
                if self._step >= 1:
                    self._documents()
                if self._step == 2:
                    self._show_review()
                self._navigate(min(3, self._step + 1))
            elif event.button.id == "setup-save":
                if self._existing and self._review_revision is None:
                    raise ConfigurationError("Refresh mappings and review a valid configuration before enrolling.")
                self._saving = True
                event.button.disabled = True
                if self._existing:
                    if self._existing_version == 2:
                        migrated = await asyncio.to_thread(self.workspace.migrate, review_revision=self._review_revision)
                        self.workspace.config_path = Path(migrated["configuration_path"])
                        self.workspace.bindings_path = Path(migrated["bindings_path"])
                    else:
                        await asyncio.to_thread(self.workspace.enroll, review_revision=self._review_revision)
                    with _operation_lease(self.workspace.config_path, self.workspace.bindings_path) as enrolled:
                        service = enrolled
                elif self.query_one("#setup-use-json", Checkbox).value:
                    service = await asyncio.to_thread(self.workspace.setup,
                        self.query_one("#setup-configuration", TextArea).text,
                        self.query_one("#setup-bindings", TextArea).text)
                else:
                    policy, bindings = self._documents()
                    service = await asyncio.to_thread(self._create, policy, bindings)
                self.exit(service)
        except (ConfigurationError, ServiceError, OSError, UnicodeError) as error:
            event.button.remove_class("-active")
            status.update(f"{error} Your form values have been kept.")
        finally:
            self._saving = False
            self.query_one("#setup-save", Button).disabled = self._existing and self._review_revision is None

    def action_help(self):
        focused = self.focused
        self.query_one("#setup-status", Static).update(str(getattr(focused, "tooltip", None) or "Use Tab to move, Enter to activate, and Back to review earlier choices. Nothing is saved until Review and enroll."))

    def on_descendant_focus(self, event):
        if event.widget.tooltip and self.is_mounted:
            self.query_one("#setup-status", Static).update(str(event.widget.tooltip))

    async def action_quit(self):
        if self._saving:
            self.query_one("#setup-status", Static).update("Saving private configuration; wait for the result before closing.")
        else:
            self.exit()
