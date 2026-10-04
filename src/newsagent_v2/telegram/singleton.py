"""Windows-safe single-instance guard for V5 Telegram bot.

Uses a lock file with process ID to detect and prevent duplicate instances.
Handles stale locks from crashed processes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Lock file location - same directory as bot for visibility
LOCK_FILE = Path(__file__).parent.parent.parent.parent / ".v5_bot_lock"

# PROCESS_QUERY_LIMITED_INFORMATION — enough to detect liveness without
# needing terminate rights (OpenProcess(1) = PROCESS_TERMINATE fails
# spuriously on STATUS_DELETE_PENDING / protected PIDs).
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class SingletonError(RuntimeError):
    """Raised when another V5 bot instance is already running."""
    pass


def _pid_is_running(pid: int) -> bool:
    """Return True if *pid* appears to be a live process."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            kernel.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack signal rights — treat as running.
        return True


def acquire_singleton_lock() -> bool:
    """Acquire singleton lock for V5 Telegram bot.
    
    Returns True if lock acquired.
    Raises SingletonError if another instance is running.
    
    Checks:
    1. If lock file exists, read PID
    2. If PID is running, refuse (another bot active)
    3. If PID not running, steal lock (stale lock from crash)
    4. Write current PID to lock file
    """
    current_pid = os.getpid()
    
    if LOCK_FILE.exists():
        try:
            stale_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            
            if _pid_is_running(stale_pid) and stale_pid != current_pid:
                raise SingletonError(
                    f"V5 Telegram bot already running (PID {stale_pid}). "
                    "Refusing second instance."
                )
            
            # Stale lock - remove it
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass
                
        except SingletonError:
            raise
        except (ValueError, OSError):
            # Corrupt lock file - remove it
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass
    
    # Acquire lock
    try:
        LOCK_FILE.write_text(str(current_pid), encoding="utf-8")
        return True
    except OSError as e:
        raise SingletonError(f"Cannot acquire singleton lock: {e}")


def release_singleton_lock() -> None:
    """Release singleton lock on clean shutdown."""
    try:
        if LOCK_FILE.exists():
            # Only remove if it's our lock
            current_pid = os.getpid()
            try:
                lock_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
                if lock_pid == current_pid:
                    LOCK_FILE.unlink()
            except (ValueError, OSError):
                pass
    except OSError:
        pass


def is_another_instance_running() -> tuple[bool, int | None]:
    """Check if another V5 bot instance is running.
    
    Returns (is_running, pid)
    """
    if not LOCK_FILE.exists():
        return False, None
    
    try:
        lock_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
        if _pid_is_running(lock_pid):
            return True, lock_pid
        return False, None
    except (ValueError, OSError):
        return False, None
