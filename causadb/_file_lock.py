"""Cross-platform file locking (flock on POSIX, msvcrt on Windows)."""
import os

try:
    import fcntl
    _POSIX = True
except ImportError:
    import msvcrt
    _POSIX = False

# Constantes compatibles con el uso actual (LOCK_EX/LOCK_SH/LOCK_UN).
if _POSIX:
    LOCK_EX = fcntl.LOCK_EX
    LOCK_SH = fcntl.LOCK_SH
    LOCK_UN = fcntl.LOCK_UN
else:
    LOCK_EX = msvcrt.LK_LOCK      # exclusivo, con reintento (~10s)
    LOCK_SH = msvcrt.LK_RLCK      # compartido, con reintento
    LOCK_UN = msvcrt.LK_UNLCK


def lock_ex(fd) -> None:
    if _POSIX:
        fcntl.flock(fd, fcntl.LOCK_EX)
    else:
        _win_lock(fd, msvcrt.LK_LOCK)


def lock_sh(fd) -> None:
    if _POSIX:
        fcntl.flock(fd, fcntl.LOCK_SH)
    else:
        _win_lock(fd, msvcrt.LK_RLCK)


def unlock(fd) -> None:
    if _POSIX:
        fcntl.flock(fd, fcntl.LOCK_UN)
    else:
        _win_lock(fd, msvcrt.LK_UNLCK)


def _win_lock(fd, mode) -> None:
    # msvcrt.locking bloquea un RANGO desde la posición actual → seek(0).
    os.lseek(fd, 0, os.SEEK_SET)
    if os.fstat(fd).st_size == 0:
        os.write(fd, b"\x00")      # asegurar ≥1 byte para el rango
        os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, mode, 1)    # 1 byte en offset 0 = lock del archivo