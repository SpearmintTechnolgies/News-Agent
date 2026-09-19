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


class SingletonError(RuntimeError):
    """Raised when another V5 bot instance is already running."""
    pass


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
            stale_pid = int(LOCK_FILE.read_text().strip())
            
            # Check if process is actually running
            if sys.platform == "win32":
                import ctypes
                kernel = ctypes.windll.kernel32
                handle = kernel.OpenProcess(1, False, stale_pid)
                if handle:
                    kernel.CloseHandle(handle)
                    # Process exists - another bot is running
                    raise SingletonError(
                        f"V5 Telegram bot already running (PID {stale_pid}). "
                        "Refusing second instance."
                    )
            else:
                # Unix: check if process exists
                try:
                    os.kill(stale_pid, 0)
                    raise SingletonError(
                        f"V5 Telegram bot already running (PID {stale_pid}). "
                        "Refusing second instance."
                    )
                except ProcessLookupError:
                    pass  # Process doesn't exist, lock is stale
            
            # Stale lock - remove it
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass
                
        except (ValueError, OSError):
            # Corrupt lock file - remove it
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass
    
    # Acquire lock
    try:
        LOCK_FILE.write_text(str(current_pid))
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
                lock_pid = int(LOCK_FILE.read_text().strip())
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
        lock_pid = int(LOCK_FILE.read_text().strip())
        
        if sys.platform == "win32":
            import ctypes
            kernel = ctypes.windll.kernel32
            handle = kernel.OpenProcess(1, False, lock_pid)
            if handle:
                kernel.CloseHandle(handle)
                return True, lock_pid
            return False, None
        else:
            try:
                os.kill(lock_pid, 0)
                return True, lock_pid
            except ProcessLookupError:
                return False, None
    except (ValueError, OSError):
        return False, None
