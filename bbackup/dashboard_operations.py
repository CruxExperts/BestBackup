"""Registry-driven interactive forms over the same operation boundary as the CLI."""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
import json
from threading import Event
from time import monotonic
from typing import Any

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.message import Message
from textual.widgets import Button, Select, Static, TextArea

from .cli_metadata import PRODUCTION_COMMANDS
from .configuration_workspace import ConfigurationWorkspace
from .models import ConfigurationError, strict_json
from .operations import UncertainOperationError
from .production_operations import execute_operation
from .progress import format_progress, observe_progress
from .service import BackupService


class EngineProgress(Message, bubble=False):
    def __init__(self, data: dict[str, Any]):
        super().__init__()
        self.data = data


class OperationPanel(VerticalScroll):
    """Keep execution, cancellation, and approvals identical across native forms."""

    DEFAULT_CSS = """
    OperationPanel { height: auto; min-height: 20; border-top: solid $panel; margin-top: 1; }
    OperationPanel TextArea { height: 9; margin-bottom: 1; }
    OperationPanel Static { height: auto; }
    OperationPanel .operation-status { margin-top: 1; }
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

    @property
    def busy(self) -> bool:
        return self._operation_running

    def compose(self) -> ComposeResult:
        yield Select([(name, name) for name in self.commands], value=self._command,
                     allow_blank=False, id="operation-select")
        yield Static("", id="operation-summary", markup=False)
        yield Static("JSON inputs: fill every required field. Approval booleans default to false.", markup=False)
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

    def _load_command(self, command: str):
        self._command = command
        definition = PRODUCTION_COMMANDS[command]
        defaults = {name: False if kind == "boolean" else 100 if kind == "limit" else 0 if kind == "offset" else ""
                    for name, kind in definition["fields"].items() if kind != "optional-string"}
        self.query_one("#operation-summary", Static).update(definition["summary"])
        self.query_one("#operation-input", TextArea).load_text(json.dumps(defaults, indent=2))
        self.query_one("#operation-status", Static).update("Review exact inputs before running; operations are never replayed automatically.")
        self.query_one("#operation-result", Static).update("")

    def prepare(self, command: str, values: dict[str, object]):
        if self.busy or command not in self.commands:
            return
        with self.prevent(Select.Changed):
            self.query_one("#operation-select", Select).value = command
        self._load_command(command)
        self.query_one("#operation-input", TextArea).load_text(json.dumps(values, indent=2))

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "operation-cancel":
            self._cancel.set()
            self.query_one("#operation-status", Static).update("Cancellation requested. Inspect retained output and operation state before retrying.")
        elif event.button.id == "operation-run" and not self.busy:
            try:
                document = self.query_one("#operation-input", TextArea).text
                if len(document.encode("utf-8")) > 1024 * 1024:
                    raise ConfigurationError("Input exceeds one MiB")
                values = strict_json(document)
            except (ConfigurationError, UnicodeError):
                self.query_one("#operation-status", Static).update("Invalid JSON input. Nothing was dispatched.")
                return
            self._cancel = Event()
            self._started = monotonic()
            self._operation_running = True
            self._progress_latest = ""
            self.query_one("#operation-run", Button).disabled = True
            self.query_one("#operation-cancel", Button).disabled = False
            self.query_one("#operation-select", Select).disabled = True
            self.query_one("#operation-input", TextArea).read_only = True
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
        except ConfigurationError:
            status.update("Invalid input or missing explicit approval. No automatic retry was performed.")
        except UncertainOperationError as error:
            status.update(f"Requires reconciliation: operation {error.operation_id}. Inspect the exact target before recording an outcome; do not replay.")
        except Exception:
            status.update("Operation did not complete. Inspect activity and retained output before retrying. Raw diagnostics and credentials are omitted.")
        else:
            status.update(f"Completed {command}. Review the result; a successful command is not a production qualification claim.")
            self.query_one("#operation-result", Static).update(json.dumps(result, indent=2, sort_keys=True))
        finally:
            self._operation_running = False
            self.query_one("#operation-run", Button).disabled = False
            self.query_one("#operation-cancel", Button).disabled = True
            self.query_one("#operation-select", Select).disabled = False
            self.query_one("#operation-input", TextArea).read_only = False
            self._completed()

    async def cancel_and_wait(self):
        if self.busy and self._worker is not None:
            self._cancel.set()
            await self._worker.wait()
