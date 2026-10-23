"""Registry-driven interactive forms over the same operation boundary as the CLI."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
import json
from pathlib import Path
import shutil
import sys
from threading import Event
from time import monotonic
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Checkbox, Collapsible, Input, Label, Select, Static, TextArea

from .cli_metadata import PRODUCTION_COMMANDS
from .configuration_workspace import ConfigurationWorkspace
from .models import ConfigurationError, strict_json
from .operations import UncertainOperationError
from .production_operations import execute_operation, validate_operation_input
from .progress import format_progress, observe_progress
from .service import BackupService, ReplicaJobError


class EngineProgress(Message, bubble=False):
    def __init__(self, data: dict[str, Any]):
        super().__init__()
        self.data = data


class OperationPanel(VerticalScroll):
    """Keep execution, cancellation, and approvals identical across native forms."""

    DEFAULT_CSS = """
    OperationPanel { height: 1fr; min-height: 8; border-top: solid $panel; margin-top: 1; }
    OperationPanel TextArea { height: 9; margin-bottom: 1; }
    OperationPanel Static { height: auto; }
    OperationPanel .operation-status { margin-top: 1; }
    OperationPanel .operation-field { height: auto; margin-bottom: 1; }
    OperationPanel .field-help { color: $text-muted; }
    """

    def __init__(self, commands: Sequence[str], service: Callable[[], BackupService],
                 workspace: ConfigurationWorkspace | None,
                 completed: Callable[[], None], *, id: str | None = None):
        super().__init__(id=id)
        self.commands = tuple(commands)
        self._service = service
        self.workspace = workspace
        self._completed = completed
        self._cancel = Event()
        self._worker = None
        self._operation_running = False
        self._started = 0.0
        self._command = self.commands[0]
        self._progress_latest = ""
        self._loading_form = False
        self._field_kinds = {name: kind for command in self.commands
                             for name, kind in PRODUCTION_COMMANDS[command]["fields"].items()}

    @property
    def busy(self) -> bool:
        return self._operation_running

    def compose(self) -> ComposeResult:
        yield Select([(PRODUCTION_COMMANDS[name].get("label", name), name) for name in self.commands], value=self._command,
                     allow_blank=False, id="operation-select")
        yield Static("", id="operation-summary", markup=False)
        with Vertical(id="operation-fields"):
            for name, kind in self._field_kinds.items():
                label = name.replace("_", " ").capitalize()
                with Vertical(id=f"field-{name}", classes="operation-field"):
                    if kind == "boolean":
                        yield Checkbox(label, id=f"value-{name}")
                    else:
                        yield Label(label, id=f"label-{name}")
                        yield Input(id=f"value-{name}", type="integer" if kind in ("limit", "offset") else "text")
                        yield Select([], id=f"choice-{name}", prompt=f"Choose {label.lower()}")
                    yield Static("", id=f"help-{name}", classes="field-help", markup=False)
        with Collapsible(title="Advanced: JSON input", collapsed=True):
            yield Checkbox("Use JSON instead of the form", id="use-json")
            yield TextArea("", id="operation-input")
        with Horizontal(classes="toolbar"):
            yield Button("Run selected operation", id="operation-run", variant="primary")
            yield Button("Cancel operation", id="operation-cancel", disabled=True)
        yield Static("", id="operation-status", classes="operation-status", markup=False)
        yield Static("", id="operation-result", markup=False)

    def on_mount(self):
        self._load_command(self._command)
        self.set_interval(0.5, self._clock)

    def on_select_changed(self, event: Select.Changed):
        if event.select.id == "operation-select":
            event.stop()
            if self.is_mounted and isinstance(event.value, str):
                self._load_command(event.value)
        elif event.select.id and event.select.id.startswith("choice-"):
            event.stop()
            self._validate_form()

    def _load_command(self, command: str):
        self._loading_form = True
        self._command = command
        definition = PRODUCTION_COMMANDS[command]
        defaults = {name: False if kind == "boolean" else 100 if kind == "limit" else 0 if kind == "offset" else ""
                    for name, kind in definition["fields"].items() if kind != "optional-string"}
        if "executable" in defaults:
            candidate = shutil.which("bbackup") or str(Path(sys.executable).parent / "bbackup")
            defaults["executable"] = candidate if Path(candidate).is_file() else ""
        self.query_one("#operation-summary", Static).update(definition["summary"])
        self.query_one("#operation-input", TextArea).load_text(json.dumps(defaults, indent=2))
        self.query_one("#use-json", Checkbox).value = False
        ui = definition.get("ui", {})
        for name, kind in self._field_kinds.items():
            visible = name in definition["fields"]
            self.query_one(f"#field-{name}").display = visible
            if not visible:
                continue
            hint = ui.get(name, {})
            help_text = hint.get("help", self._field_help(name))
            widget = self.query_one(f"#value-{name}")
            widget.tooltip = help_text
            if isinstance(widget, Checkbox):
                widget.value = False
                widget.label = hint.get("label", name.replace("_", " ").capitalize())
            else:
                self.query_one(f"#label-{name}", Label).update(hint.get("label", name.replace("_", " ").capitalize()))
                widget.value = str(defaults.get(name, ""))
                widget.placeholder = hint.get("example", "Optional" if kind == "optional-string" else "Required")
                choices = self._choices(command, name)
                select = self.query_one(f"#choice-{name}", Select)
                select.set_options([(value, value) for value in choices])
                select.value = Select.NULL
                select.tooltip = help_text
                select.display = bool(choices)
                widget.display = not choices
            self.query_one(f"#help-{name}", Static).update(help_text)
        self.query_one("#operation-status", Static).update("Complete the fields above. Required inputs are checked before anything runs.")
        self.query_one("#operation-result", Static).update("")
        self._loading_form = False
        self._validate_form()

    @staticmethod
    def _field_help(name: str) -> str:
        return {
            "snapshot": "Select the full snapshot ID from Snapshots. Short prefixes are not accepted.",
            "target": "Use a new absolute path, separate from sources, repositories, passwords and state.",
            "executable": "Absolute path to the installed bbackup executable used by the timer.",
            "source": "The source named by this operation; choose an existing configured entry.",
            "destination": "The repository that receives this copy. The source is kept unchanged.",
            "approved": "Approve only the exact plan you reviewed. This never enables automatic pruning.",
            "outcome": "Record the outcome you verified. Reconciliation does not repeat the operation.",
            "operation_id": "Copy the full operation ID from Activity; inspect its exact target first.",
            "password_file": "Reference a private password file. Never paste the password itself.",
            "limit": "Number of entries, from 1 to 1000.",
            "offset": "Skip this many entries; use 0 for the first page.",
        }.get(name, "Review this input before running; credentials and raw diagnostics are kept out of results.")

    def _choices(self, command: str, name: str) -> list[str]:
        service = self._service()
        if name == "name":
            if command.startswith(("jobs ", "service ")):
                return [job.name for job in service.config.jobs]
            if command.startswith("repositories "):
                return [repository.name for repository in service.config.repositories]
        if name == "job":
            return [job.name for job in service.config.jobs]
        if name in ("repository", "destination") or (name == "source" and command in ("snapshots copy", "repositories init")):
            return [repository.name for repository in service.config.repositories]
        if name == "outcome":
            return ["succeeded", "failed"]
        return []

    def _form_values(self):
        if self.query_one("#use-json", Checkbox).value:
            document = self.query_one("#operation-input", TextArea).text
            if len(document.encode("utf-8")) > 1024 * 1024:
                raise ConfigurationError("Input exceeds one MiB")
            return validate_operation_input(self._command, strict_json(document))
        values = {}
        for name, kind in PRODUCTION_COMMANDS[self._command]["fields"].items():
            widget = self.query_one(f"#value-{name}")
            if isinstance(widget, Checkbox):
                values[name] = widget.value
                continue
            select = self.query_one(f"#choice-{name}", Select)
            value = str(select.value) if select.display and select.value is not Select.NULL else widget.value.strip() if widget.display else ""
            if not value and kind == "optional-string":
                continue
            if not value:
                raise ConfigurationError(f"Choose or enter {name.replace('_', ' ')}.")
            if kind in ("limit", "offset"):
                try:
                    values[name] = int(value)
                except ValueError:
                    raise ConfigurationError(f"{name.capitalize()} must be a whole number.") from None
            else:
                values[name] = value
            if kind == "path" and not Path(value).is_absolute():
                raise ConfigurationError(f"{name.capitalize()} must be an absolute path.")
        return validate_operation_input(self._command, values)

    def _validate_form(self):
        if self._loading_form or not self.is_mounted or self.busy:
            return
        try:
            values = self._form_values()
        except (ConfigurationError, UnicodeError) as error:
            self.query_one("#operation-run", Button).disabled = True
            self.query_one("#operation-status", Static).update(str(error))
        else:
            self.query_one("#operation-run", Button).disabled = False
            if not self.query_one("#use-json", Checkbox).value:
                self.query_one("#operation-input", TextArea).load_text(json.dumps(values, indent=2))
            self.query_one("#operation-status", Static).update("Ready. Review the inputs, then run this operation once.")

    def on_input_changed(self, _event: Input.Changed):
        self._validate_form()

    def on_checkbox_changed(self, _event: Checkbox.Changed):
        self._validate_form()

    def on_text_area_changed(self, _event: TextArea.Changed):
        if self.is_mounted and self.query_one("#use-json", Checkbox).value:
            self._validate_form()

    def prepare(self, command: str, values: dict[str, object]):
        if self.busy or command not in self.commands:
            return
        with self.prevent(Select.Changed):
            self.query_one("#operation-select", Select).value = command
        self._load_command(command)
        self.query_one("#operation-input", TextArea).load_text(json.dumps(values, indent=2))
        self._loading_form = True
        for name, value in values.items():
            if name not in PRODUCTION_COMMANDS[command]["fields"]:
                continue
            widget = self.query_one(f"#value-{name}")
            if isinstance(widget, Checkbox):
                widget.value = bool(value)
            else:
                select = self.query_one(f"#choice-{name}", Select)
                if select.display and value in self._choices(command, name):
                    select.value = value
                else:
                    widget.value = str(value)
        self._loading_form = False
        self._validate_form()

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "operation-cancel":
            self._cancel.set()
            self.query_one("#operation-status", Static).update("Cancellation requested. Inspect retained output and operation state before retrying.")
        elif event.button.id == "operation-run" and not self.busy:
            self.start()

    def start(self):
        """Start the validated visible operation through the shared dispatcher."""
        if self.busy:
            return
        try:
            values = self._form_values()
        except (ConfigurationError, UnicodeError) as error:
            self.query_one("#operation-status", Static).update(f"{error} Nothing was dispatched.")
            return
        self._running_values = values
        self._cancel = Event()
        self._started = monotonic()
        self._operation_running = True
        self._progress_latest = ""
        self.query_one("#operation-run", Button).disabled = True
        self.query_one("#operation-cancel", Button).disabled = False
        self.query_one("#operation-select", Select).disabled = True
        self.query_one("#operation-input", TextArea).read_only = True
        self.query_one("#operation-fields").disabled = True
        self.query_one("#operation-result", Static).update("")
        self._clock()
        self._worker = self.run_worker(self._run(self._command, values),
                                       group="operation", exit_on_error=False)

    def _clock(self):
        if self.busy and not self._cancel.is_set():
            self.query_one("#operation-status", Static).update(
                f"Running {self._command}; elapsed {monotonic() - self._started:.1f}s. "
                f"{self._progress_latest or 'Waiting for numeric engine progress.'} "
                "Activity shows durable lifecycle state. Navigation remains available."
            )

    def _report_progress(self, data):
        self.post_message(EngineProgress(data))

    def on_engine_progress(self, message: EngineProgress):
        if self.busy:
            self._progress_latest = format_progress(message.data)
            self._clock()

    async def _run(self, command, values):
        status = self.query_one("#operation-status", Static)
        try:
            with observe_progress(self._report_progress):
                result = await asyncio.to_thread(
                    execute_operation, command, values, service=self._service(),
                    config_path=self.workspace.config_path if self.workspace else None,
                    bindings_path=self.workspace.bindings_path if self.workspace else None,
                    cancel=self._cancel,
                )
        except ConfigurationError as error:
            status.update(f"{error} Correct the highlighted inputs before running again.")
        except UncertainOperationError as error:
            status.update(f"Requires reconciliation: operation {error.operation_id}. Inspect the exact target before recording an outcome; do not replay.")
        except ReplicaJobError as error:
            status.update("Local backup completed, but at least one additional copy did not. Review each destination below; inspect failed or uncertain copies before retrying.")
            self.query_one("#operation-result", Static).update(json.dumps(error.result, indent=2, sort_keys=True))
        except Exception:
            status.update("Operation did not complete. Inspect activity and retained output before retrying. Raw diagnostics and credentials are omitted.")
        else:
            status.update(f"Completed {command}. Review the result; a successful command is not a production qualification claim.")
            self.query_one("#operation-result", Static).update(json.dumps(result, indent=2, sort_keys=True))
        finally:
            self._operation_running = False
            self.query_one("#operation-fields").disabled = False
            self.query_one("#operation-cancel", Button).disabled = True
            self.query_one("#operation-select", Select).disabled = False
            self.query_one("#operation-input", TextArea).read_only = False
            self._completed()

    async def cancel_and_wait(self):
        if self.busy and self._worker is not None:
            self._cancel.set()
            await self._worker.wait()
