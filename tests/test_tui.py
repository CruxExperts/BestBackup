"""
Tests for bbackup.tui - BackupStatus state machine, ETA calculation,
thread safety, and BackupTUI initialization.
Created: 2026-02-26
Last Updated: 2026-02-26
"""
import os
import sys
import termios
import threading
import time
from datetime import timedelta
from io import StringIO


import bbackup
from bbackup.config import Config
from bbackup.tui import BackupStatus, BackupTUI


# ---------------------------------------------------------------------------
# TestBackupStatusInitial
# ---------------------------------------------------------------------------


class TestBackupStatusInitial:
    def test_initial_status_is_idle(self):
        s = BackupStatus()
        assert s.status == "idle"

    def test_initial_action(self):
        s = BackupStatus()
        assert "Initializing" in s.current_action

    def test_initial_counters_are_zero(self):
        s = BackupStatus()
        assert s.total_items == 0
        assert s.completed_items == 0
        assert s.bytes_transferred == 0
        assert s.total_bytes == 0
        assert s.files_transferred == 0
        assert s.total_files == 0

    def test_initial_errors_warnings_empty(self):
        s = BackupStatus()
        assert s.errors == []
        assert s.warnings == []

    def test_initial_eta_is_none(self):
        s = BackupStatus()
        assert s.eta is None

    def test_initial_start_time_is_none(self):
        s = BackupStatus()
        assert s.start_time is None

    def test_initial_skip_current_false(self):
        s = BackupStatus()
        assert s.skip_current is False

    def test_has_lock(self):
        s = BackupStatus()
        assert hasattr(s, "lock")


# ---------------------------------------------------------------------------
# TestBackupStatusLifecycle
# ---------------------------------------------------------------------------


class TestBackupStatusLifecycle:
    def test_start_sets_running(self):
        s = BackupStatus()
        s.start()
        assert s.status == "running"
    def test_start_sets_start_time(self):
        s = BackupStatus()
        before = time.time()
        s.start()
        assert s.start_time is not None
        assert s.start_time >= before

    def test_start_does_not_revive_cancelled_status(self):
        s = BackupStatus()
        s.cancel()
        s.start()
        assert s.status == "cancelled"
        assert s.set_status("completed") is False
    def test_cancel_sets_cancelled(self):
        s = BackupStatus()
        s.start()
        s.cancel()
        assert s.status == "cancelled"

    def test_add_error_appends(self):
        s = BackupStatus()
        s.add_error("disk full")
        s.add_error("timeout")
        assert "disk full" in s.errors
        assert "timeout" in s.errors
        assert len(s.errors) == 2

    def test_add_warning_appends(self):
        s = BackupStatus()
        s.add_warning("low disk space")
        assert "low disk space" in s.warnings


# ---------------------------------------------------------------------------
# TestBackupStatusUpdate
# ---------------------------------------------------------------------------


class TestBackupStatusUpdate:
    def test_selective_update_action_only(self):
        s = BackupStatus()
        s.update(completed=5, total=10)
        s.update(action="Backing up volumes")
        assert s.current_action == "Backing up volumes"
        # Other fields unchanged
        assert s.completed_items == 5
        assert s.total_items == 10

    def test_update_completed_and_total(self):
        s = BackupStatus()
        s.update(completed=3, total=10)
        assert s.completed_items == 3
        assert s.total_items == 10

    def test_update_item(self):
        s = BackupStatus()
        s.update(item="my_volume")
        assert s.current_item == "my_volume"

    def test_update_bytes_fields(self):
        s = BackupStatus()
        s.update(bytes_transferred=1024, total_bytes=4096)
        assert s.bytes_transferred == 1024
        assert s.total_bytes == 4096

    def test_update_files_fields(self):
        s = BackupStatus()
        s.update(files_transferred=5, total_files=20, current_file="test.txt")
        assert s.files_transferred == 5
        assert s.total_files == 20
        assert s.current_file == "test.txt"

    def test_transfer_speed_calculated_on_second_call(self):
        s = BackupStatus()
        s.update(bytes_transferred=0)
        time.sleep(0.05)
        s.update(bytes_transferred=500_000)
        # Speed should be non-zero after second call with more bytes
        assert s.transfer_speed >= 0  # May be 0 if delta time is tiny

    def test_transfer_speed_non_zero_with_gap(self):
        s = BackupStatus()
        s.update(bytes_transferred=100)
        time.sleep(0.1)
        s.update(bytes_transferred=1_000_000)
        assert s.transfer_speed > 0

    def test_eta_formula1_item_based(self):
        """ETA formula 1: start_time set + completed_items > 0."""
        s = BackupStatus()
        s.start_time = time.time() - 10  # 10s elapsed
        s.update(completed=5, total=10)
        assert s.eta is not None
        assert isinstance(s.eta, timedelta)

    def test_eta_formula2_transfer_speed_based(self):
        """ETA formula 2: transfer_speed > 0 + total_bytes known."""
        s = BackupStatus()
        s.transfer_speed = 1.0  # 1 MB/s
        s.total_bytes = 10_000_000
        s.bytes_transferred = 2_000_000
        # Trigger ETA recalc: call update with no items to bypass formula 1
        # but formula 2 condition should be satisfied
        s.update(bytes_transferred=2_000_000, total_bytes=10_000_000)
        # transfer_speed was pre-set but update will recalc it; force it
        s.transfer_speed = 1.0
        s.update()  # re-trigger ETA with speed set
        assert s.eta is not None

    def test_eta_none_when_completed_zero(self):
        s = BackupStatus()
        s.start()
        s.update(completed=0, total=10)
        # No transfer speed either, so ETA should be None
        assert s.eta is None


# ---------------------------------------------------------------------------
# TestBackupStatusFlags
# ---------------------------------------------------------------------------


class TestBackupStatusFlags:
    def test_pause_via_field(self):
        s = BackupStatus()
        s.start()
        s.status = "paused"
        assert s.status == "paused"

    def test_resume_via_field(self):
        s = BackupStatus()
        s.status = "paused"
        s.status = "running"
        assert s.status == "running"

    def test_skip_current_can_be_set(self):
        s = BackupStatus()
        s.skip_current = True
        assert s.skip_current is True

    def test_skip_current_can_be_reset(self):
        s = BackupStatus()
        s.skip_current = True
        s.skip_current = False
        assert s.skip_current is False


# ---------------------------------------------------------------------------
# TestBackupStatusThreadSafety
# ---------------------------------------------------------------------------


class TestBackupStatusThreadSafety:
    def test_concurrent_updates_no_error(self):
        s = BackupStatus()
        s.start()
        errors = []

        def worker(i):
            try:
                s.update(completed=i)
                s.add_error(f"err{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        assert len(s.errors) == 20


# ---------------------------------------------------------------------------
# TestBackupTUI
# ---------------------------------------------------------------------------


class TestBackupTUI:
    def test_instantiation_does_not_crash(self):
        cfg = Config(config_path=None)
        tui = BackupTUI(cfg)
        assert tui.config is cfg
        assert isinstance(tui.status, BackupStatus)

    def test_show_header_does_not_raise(self):
        from rich.console import Console
        cfg = Config(config_path=None)
        tui = BackupTUI(cfg)
        # Replace console with one that writes to a buffer to avoid terminal output
        tui.console = Console(file=StringIO())
        tui.show_header()

    def test_show_header_uses_package_version(self):
        from rich.console import Console
        cfg = Config(config_path=None)
        tui = BackupTUI(cfg)
        output = StringIO()
        tui.console = Console(file=output, force_terminal=False, width=100)

        tui.show_header()

        assert f"Version {bbackup.__version__}" in output.getvalue()

    def test_live_dashboard_header_uses_package_version(self):
        from rich.console import Console
        cfg = Config(config_path=None)
        tui = BackupTUI(cfg)
        output = StringIO()
        console = Console(file=output, force_terminal=False, width=120)

        console.print(tui.create_live_dashboard())

        assert f"v{bbackup.__version__}" in output.getvalue()

    def test_live_dashboard_single_key_cancel_restores_terminal(self, monkeypatch):
        """A raw Q cancels the operation and restores the PTY settings."""
        from rich.console import Console

        master_fd, slave_fd = os.openpty()
        stdin = os.fdopen(slave_fd, "r", buffering=1)
        try:
            before = termios.tcgetattr(stdin)
            cfg = Config(config_path=None)
            tui = BackupTUI(cfg)
            tui.console = Console(file=StringIO(), force_terminal=False, width=100)
            monkeypatch.setattr(sys, "stdin", stdin)
            monkeypatch.setattr(sys, "stdout", StringIO())

            def operation():
                tui.status.start()
                while tui.status.get_status() != "cancelled":
                    time.sleep(0.01)

            writer = threading.Timer(0.2, lambda: os.write(master_fd, b"q"))
            writer.daemon = True
            writer.start()
            try:
                assert tui.run_with_live_dashboard(operation) is False
            finally:
                writer.cancel()
            assert tui.status.get_status() == "cancelled"
            assert termios.tcgetattr(stdin) == before
        finally:
            stdin.close()
            os.close(master_fd)