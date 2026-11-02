"""Crash-consistent file operations, locking, and signal trapping for Hermes Mod Engine."""
from __future__ import annotations

import fcntl
import os
import signal
import sys
import time
from pathlib import Path
from typing import Callable, Optional


class FileLock:
    """Inter-process file lock using fcntl.flock with spin loop and timeout.

    Protects shared JSON resources (secrets.json, .state.json, .commands_cache.json)
    against race conditions from concurrent processes (gateway, cron, CLI, subagents).
    Uses a dedicated sidecar lock file (.filename.lock) to prevent inode-replacement races
    when files are written atomically via os.replace.
    """

    def __init__(self, target_path: Path, timeout: float = 5.0, lock_path: Optional[Path] = None):
        target = Path(target_path)
        if lock_path:
            self.lock_path = Path(lock_path)
        elif target.name.endswith(".lock"):
            self.lock_path = target
        else:
            self.lock_path = target.parent / f".{target.name}.lock"
        self.timeout = float(timeout)
        self.fd: Optional[int] = None
        self._file_obj = None

    def __enter__(self):
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Open in "a" mode so file is created if missing and not truncated
        self._file_obj = open(self.lock_path, "a")
        self.fd = self._file_obj.fileno()
        start = time.time()
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError):
                if time.time() - start > self.timeout:
                    if self._file_obj:
                        try:
                            self._file_obj.close()
                        except OSError:
                            pass
                        self._file_obj = None
                    raise TimeoutError(f"Could not acquire lock on {self.lock_path} within {self.timeout}s")
                time.sleep(0.02)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._file_obj:
            try:
                if self.fd is not None:
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._file_obj.close()
            except OSError:
                pass
            self._file_obj = None
            self.fd = None


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """Atomically write text to path using temporary file and atomic replace.

    Guarantees crash-consistency:
    1. Writes to temporary file on the same filesystem.
    2. Calls flush() and os.fsync() to commit bytes to disk.
    3. Preserves existing file mode/permissions (e.g. 0600 on secrets.json).
    4. Replaces target file atomically via os.replace().
    5. Cleans up temp file if any error occurs.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    temp_path = p.parent / f".tmp_{p.name}.{os.getpid()}_{time.time_ns()}"
    try:
        with open(temp_path, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

        if p.exists():
            try:
                stat_res = p.stat()
                os.chmod(temp_path, stat_res.st_mode)
            except OSError:
                pass

        os.replace(temp_path, p)
    except Exception:
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


class SignalTrap:
    """Traps SIGINT and SIGTERM during critical disk operations and invokes rollback before exit."""

    def __init__(self, rollback_fn: Callable[[], Any]):
        self.rollback_fn = rollback_fn
        self.old_sigint = None
        self.old_sigterm = None
        self.triggered = False

    def _handler(self, signum, frame):
        self.triggered = True
        try:
            self.rollback_fn()
        except Exception as exc:
            sys.stderr.write(f"Error during signal rollback: {exc}\n")

        # Restore original handlers
        if self.old_sigint is not None:
            try:
                signal.signal(signal.SIGINT, self.old_sigint)
            except (ValueError, OSError):
                pass
        if self.old_sigterm is not None:
            try:
                signal.signal(signal.SIGTERM, self.old_sigterm)
            except (ValueError, OSError):
                pass

        if signum == signal.SIGINT:
            raise KeyboardInterrupt()
        else:
            sys.exit(128 + signum)

    def __enter__(self):
        try:
            self.old_sigint = signal.signal(signal.SIGINT, self._handler)
            self.old_sigterm = signal.signal(signal.SIGTERM, self._handler)
        except (ValueError, AttributeError):
            # Not in main thread or signal not supported
            pass
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if self.old_sigint is not None:
                signal.signal(signal.SIGINT, self.old_sigint)
            if self.old_sigterm is not None:
                signal.signal(signal.SIGTERM, self.old_sigterm)
        except (ValueError, AttributeError):
            pass
