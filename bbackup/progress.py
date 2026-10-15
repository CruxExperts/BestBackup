"""Opt-in, bounded, path-free restic progress shared by CLI and dashboard."""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
from typing import Any


ProgressCallback = Callable[[dict[str, Any]], None]
_observer: ContextVar[ProgressCallback | None] = ContextVar("bbackup_progress", default=None)
_MAX_LINE = 64 * 1024
_COUNTERS = ("total_files", "files_done", "total_bytes", "bytes_done",
             "total_files_processed", "total_bytes_processed")


@contextmanager
def observe_progress(callback: ProgressCallback) -> Iterator[None]:
    token = _observer.set(callback)
    try:
        yield
    finally:
        _observer.reset(token)


def stdout_progress_handler(operation_id: str, label: str,
                            sink: Callable[[bytes], object] | None = None) -> Callable[[bytes], object] | None:
    """Return the existing sink unchanged unless numeric engine progress is wanted."""
    observer = _observer.get()
    if observer is None or label not in {"capture", "copy", "check", "restore"}:
        return sink
    pending = bytearray()
    discarding = False

    def feed(chunk: bytes):
        nonlocal discarding
        if sink is not None:
            sink(chunk)
        for fragment in chunk.splitlines(keepends=True):
            complete = fragment.endswith(b"\n")
            if discarding:
                if complete:
                    discarding = False
                continue
            if len(pending) + len(fragment) > _MAX_LINE:
                pending.clear()
                discarding = not complete
                continue
            pending.extend(fragment)
            if not complete:
                continue
            try:
                value = json.loads(pending)
            except (ValueError, UnicodeDecodeError, RecursionError):
                pending.clear()
                continue
            pending.clear()
            if not isinstance(value, dict) or value.get("message_type") not in ("status", "summary"):
                continue
            data: dict[str, Any] = {"operation_id": operation_id, "label": label,
                    "message_type": value["message_type"]}
            for key in _COUNTERS:
                number = value.get(key)
                if type(number) is int and number >= 0:
                    data[key] = number
            fraction = value.get("percent_done")
            if (isinstance(fraction, (int, float)) and not isinstance(fraction, bool)
                    and 0 <= fraction <= 1 and math.isfinite(fraction)):
                data["percent_done"] = fraction
            if len(data) > 3:
                observer(data)
    return feed


def format_progress(data: dict[str, Any]) -> str:
    fields = []
    if "percent_done" in data:
        fields.append(f"{data['percent_done'] * 100:.1f}%")
    if "files_done" in data:
        total = f"/{data['total_files']}" if "total_files" in data else ""
        fields.append(f"files {data['files_done']}{total}")
    elif "total_files_processed" in data:
        fields.append(f"files processed {data['total_files_processed']}")
    if "bytes_done" in data:
        total = f"/{data['total_bytes']}" if "total_bytes" in data else ""
        fields.append(f"bytes {data['bytes_done']}{total}")
    elif "total_bytes_processed" in data:
        fields.append(f"bytes processed {data['total_bytes_processed']}")
    return " | ".join(fields)
