"""Tests del wrapper de file locking multiplataforma (``causadb._file_lock``).

RED→GREEN (Artículo III): estos tests se escriben ANTES de la
implementación de ``causadb._file_lock``. Deben fallar con ``ImportError``
hasta que el módulo exista, luego pasar.

Anti-teatro (Artículo IX): validan comportamiento REAL del wrapper —
delegación a ``fcntl.flock`` en POSIX y a ``msvcrt.locking`` en Windows —
no aserciones triviales. El path Windows se fuerza con ``monkeypatch``
sobre ``_POSIX`` sin necesitar una máquina Windows.
"""
import os
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# POSIX: el wrapper delega a fcntl.flock
# ---------------------------------------------------------------------------

def test_lock_ex_delegates_to_fcntl():
    """``lock_ex`` llama ``fcntl.flock(fd, LOCK_EX)`` en POSIX."""
    import causadb._file_lock as fl

    if not fl._POSIX:
        pytest.skip("Este test valida la rama POSIX (Linux)")

    with mock.patch.object(fl.fcntl, "flock") as mock_flock:
        fl.lock_ex(42)

    mock_flock.assert_called_once_with(42, fl.LOCK_EX)


def test_lock_sh_delegates_to_fcntl():
    """``lock_sh`` llama ``fcntl.flock(fd, LOCK_SH)`` en POSIX."""
    import causadb._file_lock as fl

    if not fl._POSIX:
        pytest.skip("Este test valida la rama POSIX (Linux)")

    with mock.patch.object(fl.fcntl, "flock") as mock_flock:
        fl.lock_sh(42)

    mock_flock.assert_called_once_with(42, fl.LOCK_SH)


def test_unlock_delegates_to_fcntl():
    """``unlock`` llama ``fcntl.flock(fd, LOCK_UN)`` en POSIX."""
    import causadb._file_lock as fl

    if not fl._POSIX:
        pytest.skip("Este test valida la rama POSIX (Linux)")

    with mock.patch.object(fl.fcntl, "flock") as mock_flock:
        fl.unlock(42)

    mock_flock.assert_called_once_with(42, fl.LOCK_UN)


# ---------------------------------------------------------------------------
# Windows: _win_lock usa msvcrt.locking sobre 1 byte en offset 0
# ---------------------------------------------------------------------------

def test_win_lock_seeks_and_locks_one_byte(tmp_path, monkeypatch):
    """``_win_lock`` hace seek(0) + locking de 1 byte (path Windows)."""
    import causadb._file_lock as fl

    # Forzar la rama Windows sin necesitar una máquina Windows.
    monkeypatch.setattr(fl, "_POSIX", False)

    lock_path = tmp_path / "test.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)

    try:
        with mock.patch.object(fl, "msvcrt", create=True) as mock_msvcrt:
            fl._win_lock(fd, fl.LOCK_EX)

        # msvcrt.locking debe llamarse con (fd, LOCK_EX, 1) — 1 byte.
        mock_msvcrt.locking.assert_called_once_with(fd, fl.LOCK_EX, 1)
        # El archivo debe tener al menos 1 byte (para el rango).
        assert os.fstat(fd).st_size >= 1
    finally:
        os.close(fd)


def test_win_lock_writes_byte_when_empty(tmp_path, monkeypatch):
    """``_win_lock`` escribe un byte si el archivo está vacío (rango)."""
    import causadb._file_lock as fl

    monkeypatch.setattr(fl, "_POSIX", False)

    lock_path = tmp_path / "empty.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
    assert os.fstat(fd).st_size == 0  # archivo vacío

    try:
        with mock.patch.object(fl, "msvcrt", create=True) as mock_msvcrt:
            fl._win_lock(fd, fl.LOCK_EX)

        # Se escribió un byte → el archivo ya no está vacío.
        assert os.fstat(fd).st_size == 1
        mock_msvcrt.locking.assert_called_once_with(fd, fl.LOCK_EX, 1)
    finally:
        os.close(fd)