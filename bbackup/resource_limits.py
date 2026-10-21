"""Remote-primary staging boundary and observed free-space monitoring.

The dedicated mount provides the hard capacity ceiling. Free-space reserves
are observations; other writers can consume space between checks.
"""
from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
import stat

from .models import ResourceBudget


class ResourceLimitError(RuntimeError):
    """A sanitized capacity preflight or monitored reserve failure."""


def _volume(path: Path) -> tuple[int, int]:
    try:
        info = os.statvfs(path)
    except OSError:
        raise ResourceLimitError("Staging or state filesystem is unavailable") from None
    return info.f_blocks * info.f_frsize, info.f_bavail * info.f_frsize


class ResourceMonitor:
    def __init__(self, staging: Path, state: Path, budget: ResourceBudget, *,
                 read_only_reconcile: bool = False):
        self.staging, self.state, self.budget = Path(staging), Path(state), budget
        self.read_only_reconcile = read_only_reconcile
        self._lock_fd = None
        if (not self.staging.is_absolute() or self.staging.resolve() != self.staging
                or not os.path.ismount(self.staging)):
            raise ResourceLimitError("Remote staging must be a dedicated mounted filesystem")
        try:
            info = self.staging.lstat()
        except OSError:
            raise ResourceLimitError("Remote staging is unavailable") from None
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise ResourceLimitError("Remote staging must be an owned private directory")
        lock = self.staging / ".bbackup-staging.lock"
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            self._lock_fd = descriptor
            lock_info = os.fstat(descriptor)
            if (not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid()
                    or lock_info.st_mode & 0o077 or lock_info.st_nlink != 1
                    or lock_info.st_dev != info.st_dev):
                raise ResourceLimitError("Remote staging lock is not an owned private file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN):
                    raise ResourceLimitError("Remote staging is busy") from None
                raise ResourceLimitError("Remote staging lock is unavailable") from None
            if not read_only_reconcile and any(path.name != lock.name for path in self.staging.iterdir()):
                raise ResourceLimitError("Remote staging must be empty before capture")
            if lock.lstat().st_ino != lock_info.st_ino:
                raise ResourceLimitError("Remote staging lock changed during acquisition")
            capacity, _ = _volume(self.staging)
        except OSError:
            self.close()
            raise ResourceLimitError("Remote staging lock or mount is unavailable") from None
        except BaseException:
            self.close()
            raise
        try:
            if capacity <= 0 or capacity > budget.staging_max_bytes:
                raise ResourceLimitError("Mounted staging capacity exceeds the configured budget")
            self.capacity = capacity
            self.device = info.st_dev
            self.lock_inode = lock_info.st_ino
            self.initial_staging_free_bytes = _volume(self.staging)[1]
            self.initial_state_free_bytes = _volume(self.state)[1]
            self.peak_staging_used_bytes = 0
            self.peak_state_used_bytes = 0
            self.minimum_staging_free_bytes = capacity
            self.minimum_state_free_bytes = 2**63 - 1
            if not read_only_reconcile:
                self.check()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._lock_fd is not None:
            descriptor, self._lock_fd = self._lock_fd, None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def check(self, upcoming_bytes: int = 0) -> None:
        if type(upcoming_bytes) is not int or upcoming_bytes < 0:
            raise ValueError("upcoming_bytes must be nonnegative")
        if self._lock_fd is None:
            raise ResourceLimitError("Remote staging lease is closed")
        lock = self.staging / ".bbackup-staging.lock"
        try:
            lock_info = lock.lstat()
        except OSError:
            raise ResourceLimitError("Remote staging lock changed during capture") from None
        if (lock_info.st_ino != self.lock_inode or lock_info.st_dev != self.device
                or not stat.S_ISREG(lock_info.st_mode)):
            raise ResourceLimitError("Remote staging lock changed during capture")
        capacity, available = _volume(self.staging)
        state_capacity, state_available = _volume(self.state)
        try:
            device = self.staging.stat().st_dev
        except OSError:
            raise ResourceLimitError("Staging mount changed during capture") from None
        if (capacity != self.capacity or device != self.device or not os.path.ismount(self.staging)):
            raise ResourceLimitError("Staging mount changed during capture")
        self.peak_staging_used_bytes = max(self.peak_staging_used_bytes,
                                           self.initial_staging_free_bytes - available)
        self.peak_state_used_bytes = max(self.peak_state_used_bytes,
                                         self.initial_state_free_bytes - state_available)
        self.minimum_staging_free_bytes = min(self.minimum_staging_free_bytes, available)
        self.minimum_state_free_bytes = min(self.minimum_state_free_bytes, state_available)
        if (available - upcoming_bytes < self.budget.minimum_free_bytes
                or state_available < self.budget.minimum_free_bytes):
            raise ResourceLimitError("Observed free-space reserve fell below the configured threshold")

    def result(self) -> dict[str, int | bool]:
        return {
            "staging_capacity_bytes": self.capacity,
            "staging_observed_peak_additional_used_bytes": self.peak_staging_used_bytes,
            "state_observed_peak_additional_used_bytes": self.peak_state_used_bytes,
            "staging_minimum_free_bytes": self.minimum_staging_free_bytes,
            "state_minimum_free_bytes": self.minimum_state_free_bytes,
            "staging_capacity_bounded": True,
            "free_space_reserve_hard_guaranteed": False,
            "usage_attributed_to_this_operation": False,
        }
