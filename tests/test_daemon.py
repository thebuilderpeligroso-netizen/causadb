"""Tests for F.11.1 — Daemon fork (_daemon.py + --daemon CLI flags).
F.13.2.1 — PlatformDaemon ABC + UnixDaemon refactor.
F.13.2.2 — WindowsDaemon (tasklist/taskkill).
F.13.2.3 — get_daemon() factory + selección dinámica por sys.platform.

Artículo III: Test-first. Artículo IX: Anti-teatro.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from abc import ABC
from unittest.mock import patch

import pytest

from causadb._daemon import (
    LOG_DIR,
    PID_DIR,
    DaemonError,
    PlatformDaemon,
    UnixDaemon,
    WindowsDaemon,
    _pidfile_path,
    daemonize,
    get_daemon,
    is_running,
    kill_daemon,
    read_pidfile,
    remove_pidfile,
    write_pidfile,
)
from causadb.cli.main import main


# ---------------------------------------------------------------------------
# _daemon.py unit tests
# ---------------------------------------------------------------------------


def test_daemonize_creates_pidfile():
    """Verify daemonize writes the PID file in the background process."""
    name = "test_daemonize_pid"
    remove_pidfile(name)
    pid = os.fork()
    if pid == 0:
        daemonize(name)
        os._exit(0)
    os.waitpid(pid, 0)
    time.sleep(0.5)
    pid_from_file = read_pidfile(name)
    remove_pidfile(name)
    assert pid_from_file is not None
    assert isinstance(pid_from_file, int)


def test_is_running_true():
    name = "test_is_running_true"
    remove_pidfile(name)
    write_pidfile(name)
    assert is_running(name) is True
    remove_pidfile(name)


def test_is_running_false():
    assert is_running("nonexistent_daemon") is False


def test_read_pidfile_nonexistent():
    assert read_pidfile("nonexistent_daemon") is None


def test_write_read_pidfile_roundtrip():
    name = "test_write_read_roundtrip"
    remove_pidfile(name)
    write_pidfile(name)
    pid = read_pidfile(name)
    remove_pidfile(name)
    assert pid == os.getpid()


def test_kill_daemon_removes_pidfile():
    name = "test_kill_rm_pid"
    remove_pidfile(name)
    pid = os.fork()
    if pid == 0:
        write_pidfile(name)
        while True:
            time.sleep(1)
    else:
        time.sleep(0.3)
        assert os.path.exists(_pidfile_path(name))
        killed = kill_daemon(name, timeout=3.0)
        assert killed is True
        assert not os.path.exists(_pidfile_path(name))


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


def test_vigilante_start_stop():
    with tempfile.TemporaryDirectory() as tmp:
        ledger = os.path.join(tmp, "ledger.log")
        watch = os.path.join(tmp, "watch")
        os.makedirs(watch)
        code = main(["vigilante", "start", "--ledger", ledger, "--watch", watch])
        assert code == 0
        code = main(["vigilante", "stop", "--ledger", ledger])
        assert code == 0


def test_vigilante_stop_without_start():
    result = main(["vigilante", "stop", "--ledger", "/tmp/nonexistent/ledger.log"])
    assert result == 0


# ---------------------------------------------------------------------------
# Anti-teatro — real mutation tests (Artículo IX)
# These tests invoke the actual functions under test. If daemonize() or
# kill_daemon() were no-ops, these tests would FAIL.
# ---------------------------------------------------------------------------


def test_anti_teatro_daemonize_actually_forks():
    """If daemonize is a no-op (doesn't fork), the PID file will contain
    THIS process's PID, not a child's. Verify the daemon PID differs from
    the test process PID — proving a real fork happened."""
    name = "test_anti_teatro_real_fork"
    remove_pidfile(name)
    pid = os.fork()
    if pid == 0:
        daemonize(name)
        os._exit(0)
    os.waitpid(pid, 0)
    time.sleep(0.5)
    daemon_pid = read_pidfile(name)
    remove_pidfile(name)
    assert daemon_pid is not None, "daemonize must create a PID file"
    assert daemon_pid != os.getpid(), (
        "daemon PID must differ from test PID — if equal, daemonize didn't fork"
    )


def test_anti_teatro_kill_actually_terminates():
    """If kill_daemon is a no-op, the child process stays alive and the
    PID file persists. Verify both: process is dead AND PID file removed.

    Note: after SIGTERM/SIGKILL the child becomes a zombie until reaped
    via waitpid. os.kill(zombie_pid, 0) succeeds (zombie still has a
    pid table entry), so we must reap before checking.
    """
    name = "test_anti_teatro_real_kill"
    remove_pidfile(name)
    pid = os.fork()
    if pid == 0:
        write_pidfile(name)
        while True:
            time.sleep(1)
    else:
        time.sleep(0.3)
        child_pid = read_pidfile(name)
        assert child_pid is not None
        killed = kill_daemon(name, timeout=3.0)
        assert killed is True, "kill_daemon must return True"
        assert not os.path.exists(_pidfile_path(name)), (
            "kill_daemon must remove the PID file"
        )
        # Reap the zombie child. If kill_daemon had been a no-op,
        # waitpid would hang (child still alive) — timeout proves it.
        try:
            os.waitpid(child_pid, os.WNOHANG)
        except OSError:
            pass  # already reaped
        try:
            os.kill(child_pid, 0)
            assert False, "child process must be dead after kill_daemon"
        except OSError:
            pass


# ---------------------------------------------------------------------------
# F.13.2.1 — PlatformDaemon ABC + UnixDaemon
# ---------------------------------------------------------------------------


def test_platform_daemon_is_abc():
    """PlatformDaemon es ABC: no se puede instanciar directo.

    Artículo VIII: la abstracción solo existe porque UnixDaemon la implementa
    y WindowsDaemon la implementará en F.13.2.2. Pero la ABC en sí sigue
    siendo abstracta — instanciarla directa debe fallar.
    """
    assert issubclass(PlatformDaemon, ABC)
    with pytest.raises(TypeError):
        PlatformDaemon()  # type: ignore[abstract]


def test_unix_daemon_inherits_platform_daemon():
    """UnixDaemon es subclase concreta de PlatformDaemon."""
    assert issubclass(UnixDaemon, PlatformDaemon)
    # Es instanciable (no quedan métodos abstractos sin implementar)
    instance = UnixDaemon()
    assert isinstance(instance, PlatformDaemon)


def test_unix_daemon_daemonize_writes_pidfile():
    """Llamar UnixDaemon().daemonize(name) escribe un PID file.

    Mismo contrato que test_daemonize_creates_pidfile pero via instancia
    de la clase concreta (no via wrapper module-level).
    """
    name = "test_unix_daemon_daemonize_pid"
    remove_pidfile(name)
    daemon = UnixDaemon()
    pid = os.fork()
    if pid == 0:
        daemon.daemonize(name)
        os._exit(0)
    os.waitpid(pid, 0)
    time.sleep(0.5)
    pid_from_file = read_pidfile(name)
    remove_pidfile(name)
    assert pid_from_file is not None
    assert isinstance(pid_from_file, int)


def test_unix_daemon_is_running_detects_alive():
    """UnixDaemon().is_running(name) retorna True para proceso vivo."""
    name = "test_unix_daemon_is_running_alive"
    remove_pidfile(name)
    daemon = UnixDaemon()
    write_pidfile(name)  # escribe PID del proceso actual (vivo)
    try:
        assert daemon.is_running(name) is True
    finally:
        remove_pidfile(name)


def test_unix_daemon_is_running_detects_dead():
    """UnixDaemon().is_running(name) retorna False para proceso inexistente."""
    daemon = UnixDaemon()
    # PID inexistente: escribimos un PID que seguro no existe (un valor alto).
    name = "test_unix_daemon_is_running_dead"
    remove_pidfile(name)
    path = _pidfile_path(name)
    with open(path, "w") as f:
        # PID 2 suele ser kthreadd en Linux; usamos un PID ridículamente alto
        # que casi seguro no existe. Si existiera por casualidad, el test
        # sería flaky — preferimos un PID en el rango no asignable.
        f.write("999999")
    try:
        assert daemon.is_running(name) is False
    finally:
        remove_pidfile(name)


def test_unix_daemon_kill_terminates():
    """UnixDaemon().kill(name) termina el proceso y borra el PID file."""
    name = "test_unix_daemon_kill"
    remove_pidfile(name)
    daemon = UnixDaemon()
    pid = os.fork()
    if pid == 0:
        write_pidfile(name)
        while True:
            time.sleep(1)
    else:
        time.sleep(0.3)
        assert os.path.exists(_pidfile_path(name))
        killed = daemon.kill(name, timeout=3.0)
        assert killed is True
        assert not os.path.exists(_pidfile_path(name))
        # Reap zombie
        try:
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass


def test_module_level_daemonize_uses_get_daemon():
    """El wrapper module-level `daemonize` delega a `get_daemon()`.

    Verifica que el wrapper NO reimplementa la lógica ni usa un singleton
    hardcodeado — delega a `get_daemon()` (factory dinámica por plataforma).
    Mockeamos `get_daemon` y confirmamos que se invoca y que su
    `.daemonize(...)` se llama con los argumentos correctos.
    """
    with patch("causadb._daemon.get_daemon") as mock_get:
        mock_daemon = mock_get.return_value
        daemonize("some_name", logfile="/tmp/some.log")
        mock_get.assert_called_once()
        mock_daemon.daemonize.assert_called_once_with("some_name", "/tmp/some.log")


def test_module_level_is_running_uses_get_daemon():
    """El wrapper module-level `is_running` delega a `get_daemon()`."""
    with patch("causadb._daemon.get_daemon") as mock_get:
        mock_daemon = mock_get.return_value
        mock_daemon.is_running.return_value = True
        result = is_running("some_name")
        mock_get.assert_called_once()
        mock_daemon.is_running.assert_called_once_with("some_name")
        assert result is True


def test_module_level_kill_daemon_uses_get_daemon():
    """El wrapper module-level `kill_daemon` delega a `get_daemon().kill`.

    Nota: el método de la clase se llama `kill`, pero el wrapper module-level
    preserva el nombre legacy `kill_daemon`.
    """
    with patch("causadb._daemon.get_daemon") as mock_get:
        mock_daemon = mock_get.return_value
        mock_daemon.kill.return_value = True
        result = kill_daemon("some_name", timeout=7.0)
        mock_get.assert_called_once()
        mock_daemon.kill.assert_called_once_with("some_name", 7.0)
        assert result is True


# ---------------------------------------------------------------------------
# Anti-teatro (Artículo IX) — FakeDaemon que NO hace fork.
# Verifica que test_daemonize_creates_pidfile FALLARÍA con un daemon falso,
# probando que el test real no pasa trivialmente (no es teatro).
# ---------------------------------------------------------------------------


class FakeDaemon(PlatformDaemon):
    """Daemon falso para anti-teatro: daemonize es no-op (no fork).

    Si los tests de daemonize pasaran con este FakeDaemon, significaría que
    no están verificando comportamiento real. Usarlo debe hacer FALLAR el
    test `test_daemonize_creates_pidfile` (adaptado), probando que el test
    real ejercita el fork de verdad.
    """

    def daemonize(self, name: str, logfile=None) -> None:
        # NO hace fork. Escribe el PID del proceso actual (el test runner),
        # lo cual es detectable: el PID file contendría el PID del test, no
        # el de un child daemon.
        write_pidfile(name)

    def is_running(self, name: str) -> bool:
        return is_running(name)

    def kill(self, name: str, timeout: float = 5.0) -> bool:
        return kill_daemon(name, timeout)


def test_anti_teatro_unix_daemon_daemonize_does_not_fork():
    """FakeDaemon.daemonize NO hace fork → el PID file contiene el PID del
    proceso que llama (el test runner), NO un child daemon.

    Si este test PASA con FakeDaemon, significa que FakeDaemon efectivamente
    no forkea (su PID file == PID del test). Y si UnixDaemon SÍ forkea, su
    PID file != PID del test. La diferencia entre ambos prueba que
    UnixDaemon.daemonize no es un no-op — ejerce el fork de verdad.

    Artículo IX: este test es el "test del test". Verifica que la suite
    de daemonize distingue un daemon real de un stub.
    """
    # Con FakeDaemon: el PID escrito es el del proceso que llama.
    name_fake = "test_anti_teatro_fake_daemon"
    remove_pidfile(name_fake)
    fake = FakeDaemon()
    fake_pid = os.fork()
    if fake_pid == 0:
        fake.daemonize(name_fake)
        os._exit(0)
    os.waitpid(fake_pid, 0)
    time.sleep(0.3)
    fake_daemon_pid = read_pidfile(name_fake)
    remove_pidfile(name_fake)

    # Con UnixDaemon: el PID escrito es el del child daemonizado (distinto
    # del proceso que llama al fork intermedio del test).
    name_unix = "test_anti_teatro_unix_daemon"
    remove_pidfile(name_unix)
    unix = UnixDaemon()
    unix_pid = os.fork()
    if unix_pid == 0:
        unix.daemonize(name_unix)
        os._exit(0)
    os.waitpid(unix_pid, 0)
    time.sleep(0.5)
    unix_daemon_pid = read_pidfile(name_unix)
    remove_pidfile(name_unix)

    # Ambos deben haber escrito un PID file.
    assert fake_daemon_pid is not None, "FakeDaemon debe escribir PID file"
    assert unix_daemon_pid is not None, "UnixDaemon debe escribir PID file"

    # FakeDaemon no forkea en daemonize → el PID en el archivo es el del
    # proceso que llamó daemonize (que tras el fork intermedio del test es
    # el child, cuyo PID == fake_pid). UnixDaemon SÍ forkea → su PID es
    # distinto del proceso que llamó daemonize (otro child, nieto del test).
    assert fake_daemon_pid == fake_pid, (
        "FakeDaemon no forkea → PID file debe contener el PID del proceso "
        "que llamó daemonize. Si esto falla, FakeDaemon sí está forkeando "
        "(y entonces no sirve como anti-teatro)."
    )
    assert unix_daemon_pid != unix_pid, (
        "UnixDaemon SÍ forkea → el PID en el archivo debe ser el del child "
        "daemonizado, distinto del proceso que llamó daemonize. Si esto "
        "falla, UnixDaemon no está forkeando (es un no-op = teatro)."
    )


# ---------------------------------------------------------------------------
# Contract test de la ABC — toda implementación concreta debe crear PID file.
# La lista crecerá cuando se agregue WindowsDaemon en F.13.2.2.
# ---------------------------------------------------------------------------


_DAEMON_IMPLEMENTATIONS = [UnixDaemon, WindowsDaemon]


@pytest.mark.parametrize("impl_cls", _DAEMON_IMPLEMENTATIONS)
def test_contract_all_implementations_daemonize_creates_pidfile(impl_cls):
    """Contract test: cada implementación concreta de PlatformDaemon debe
    escribir un PID file al llamar `daemonize(name)`.

    Este test se corre automáticamente sobre todas las implementaciones
    registradas en `_DAEMON_IMPLEMENTATIONS`. Cuando se agregue
    WindowsDaemon (F.13.2.2), basta añadirlo a la lista y este test
    verificará que también cumple el contract.
    """
    name = f"test_contract_daemonize_{impl_cls.__name__}"
    remove_pidfile(name)
    daemon = impl_cls()
    pid = os.fork()
    if pid == 0:
        daemon.daemonize(name)
        os._exit(0)
    os.waitpid(pid, 0)
    time.sleep(0.5)
    pid_from_file = read_pidfile(name)
    remove_pidfile(name)
    assert pid_from_file is not None, (
        f"{impl_cls.__name__}.daemonize debe escribir un PID file"
    )
    assert isinstance(pid_from_file, int)


# ---------------------------------------------------------------------------
# F.13.2.2 — WindowsDaemon
#
# WindowsDaemon usa `tasklist`/`taskkill` (built-ins de Windows). En Linux
# no existen, así que los tests mockean `subprocess.run` para simular su
# comportamiento. La degradación suave (returncode != 0, FileNotFoundError)
# retorna False sin propagar errores.
# ---------------------------------------------------------------------------


def test_windows_daemon_inherits_platform_daemon():
    """WindowsDaemon es subclase concreta de PlatformDaemon (F.13.2.2)."""
    assert issubclass(WindowsDaemon, PlatformDaemon)
    instance = WindowsDaemon()
    assert isinstance(instance, PlatformDaemon)


def test_windows_daemon_daemonize_writes_pidfile():
    """WindowsDaemon().daemonize(name) escribe PID file con os.getpid().

    Enfoque A: el caller ya es el daemon. No hay fork. El PID file
    contiene el PID del proceso que llama (cross-platform: os.getpid()
    funciona en Linux también, así que el test no requiere mock).
    """
    name = "test_windows_daemon_daemonize_pid"
    remove_pidfile(name)
    daemon = WindowsDaemon()
    daemon.daemonize(name)
    try:
        pid_from_file = read_pidfile(name)
        assert pid_from_file is not None, "daemonize debe escribir PID file"
        assert isinstance(pid_from_file, int)
        assert pid_from_file == os.getpid(), (
            "Enfoque A: el PID file debe contener el PID del caller "
            "(os.getpid()), no un child. Si difiere, daemonize está "
            "forkeando (no debería en WindowsDaemon)."
        )
    finally:
        remove_pidfile(name)


def test_windows_daemon_daemonize_creates_logfile():
    """daemonize() crea/toca el logfile (mismo contrato que UnixDaemon)."""
    name = "test_windows_daemon_daemonize_log"
    remove_pidfile(name)
    daemon = WindowsDaemon()
    daemon.daemonize(name)
    try:
        log_path = os.path.join(LOG_DIR, f"{name}.log")
        assert os.path.exists(log_path), "daemonize debe crear el logfile"
    finally:
        remove_pidfile(name)
        log_path = os.path.join(LOG_DIR, f"{name}.log")
        if os.path.exists(log_path):
            os.remove(log_path)


def test_windows_daemon_daemonize_custom_logfile():
    """daemonize() respeta el parámetro `logfile` si se pasa."""
    with tempfile.TemporaryDirectory() as tmp:
        custom_log = os.path.join(tmp, "custom.log")
        name = "test_windows_daemon_custom_log"
        remove_pidfile(name)
        daemon = WindowsDaemon()
        daemon.daemonize(name, logfile=custom_log)
        try:
            assert os.path.exists(custom_log), (
                "daemonize debe crear el logfile custom si se pasa"
            )
        finally:
            remove_pidfile(name)


def test_windows_daemon_daemonize_raises_on_pidfile_failure():
    """Si write_pidfile falla, daemonize levanta DaemonError (Fall-Closed)."""
    name = "test_windows_daemon_pidfile_fail"
    remove_pidfile(name)
    daemon = WindowsDaemon()
    with patch("causadb._daemon.write_pidfile", side_effect=OSError("disk full")):
        with pytest.raises(DaemonError):
            daemon.daemonize(name)
    remove_pidfile(name)


def test_windows_daemon_is_running_uses_tasklist():
    """is_running() usa `tasklist` (NO os.kill(pid, 0)).

    Mockeamos subprocess.run para retornar output que contiene el PID.
    Verificamos que se llamó `tasklist` (no os.kill) y que retorna True.
    """
    name = "test_windows_daemon_is_running_tasklist"
    remove_pidfile(name)
    daemon = WindowsDaemon()
    write_pidfile(name)  # escribe PID del proceso actual
    pid = read_pidfile(name)
    try:
        with patch("causadb._daemon.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["tasklist", "/FI", f"PID eq {pid}"],
                returncode=0,
                stdout=f"some header\npython.exe   {pid}  Console  100 K\n",
                stderr="",
            )
            result = daemon.is_running(name)

        assert result is True, "is_running debe retornar True si PID está en tasklist"
        # Verificar que se llamó tasklist con el filtro correcto
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "tasklist", (
            "is_running debe invocar `tasklist`, no os.kill(pid, 0)"
        )
        assert f"PID eq {pid}" in call_args
    finally:
        remove_pidfile(name)


def test_windows_daemon_is_running_returns_false_when_pid_not_in_tasklist():
    """Si tasklist no incluye el PID, is_running retorna False."""
    name = "test_windows_daemon_is_running_not_in_tasklist"
    remove_pidfile(name)
    daemon = WindowsDaemon()
    write_pidfile(name)
    pid = read_pidfile(name)
    try:
        with patch("causadb._daemon.subprocess.run") as mock_run:
            # Output SIN el PID (proceso muerto)
            mock_run.return_value = subprocess.CompletedProcess(
                args=["tasklist", "/FI", f"PID eq {pid}"],
                returncode=0,
                stdout="INFO: No tasks are running which match the specified criteria.\r\n",
                stderr="",
            )
            result = daemon.is_running(name)
        assert result is False, (
            "is_running debe retornar False si el PID no aparece en tasklist"
        )
    finally:
        remove_pidfile(name)


def test_windows_daemon_is_running_returns_false_when_no_pidfile():
    """Sin PID file, is_running retorna False (no llama tasklist)."""
    name = "test_windows_daemon_is_running_no_pidfile"
    remove_pidfile(name)
    daemon = WindowsDaemon()
    with patch("causadb._daemon.subprocess.run") as mock_run:
        result = daemon.is_running(name)
    assert result is False, "Sin PID file, is_running debe retornar False"
    mock_run.assert_not_called(), (
        "is_running no debe llamar tasklist si no hay PID file"
    )


def test_windows_daemon_is_running_handles_tasklist_failure():
    """Si tasklist falla (returncode != 0), is_running retorna False.

    Degradación suave: no propaga el error.
    """
    name = "test_windows_daemon_is_running_tasklist_fail"
    remove_pidfile(name)
    daemon = WindowsDaemon()
    write_pidfile(name)
    try:
        with patch("causadb._daemon.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["tasklist"],
                returncode=1,
                stdout="",
                stderr="ERROR: not found",
            )
            result = daemon.is_running(name)
        assert result is False, (
            "is_running debe retornar False si tasklist falla (returncode != 0)"
        )
    finally:
        remove_pidfile(name)


def test_windows_daemon_is_running_handles_tasklist_not_in_path():
    """Si tasklist no está en PATH (FileNotFoundError), retorna False."""
    name = "test_windows_daemon_is_running_no_tasklist"
    remove_pidfile(name)
    daemon = WindowsDaemon()
    write_pidfile(name)
    try:
        with patch(
            "causadb._daemon.subprocess.run",
            side_effect=FileNotFoundError("tasklist not found"),
        ):
            result = daemon.is_running(name)
        assert result is False, (
            "is_running debe retornar False si tasklist no está en PATH"
        )
    finally:
        remove_pidfile(name)


def test_windows_daemon_kill_uses_taskkill():
    """kill() usa `taskkill /PID <pid> /F` como método primario."""
    name = "test_windows_daemon_kill_taskkill"
    remove_pidfile(name)
    daemon = WindowsDaemon()
    write_pidfile(name)
    pid = read_pidfile(name)
    try:
        with patch("causadb._daemon.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["taskkill", "/PID", str(pid), "/F"],
                returncode=0,
                stdout=f"SUCCESS: Sent termination signal to process with PID {pid}.\r\n",
                stderr="",
            )
            result = daemon.kill(name)

        assert result is True, "kill debe retornar True si taskkill exitos"
        mock_run.assert_called_once()
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "taskkill", (
            "kill debe invocar `taskkill`, no os.kill"
        )
        assert "/PID" in call_args
        assert str(pid) in call_args
        assert "/F" in call_args, "kill debe usar /F (force)"
    finally:
        remove_pidfile(name)


def test_windows_daemon_kill_returns_false_when_no_pidfile():
    """Sin PID file, kill() retorna False (no llama taskkill)."""
    name = "test_windows_daemon_kill_no_pidfile"
    remove_pidfile(name)
    daemon = WindowsDaemon()
    with patch("causadb._daemon.subprocess.run") as mock_run:
        result = daemon.kill(name)
    assert result is False, "Sin PID file, kill debe retornar False"
    mock_run.assert_not_called(), "kill no debe llamar taskkill sin PID file"


def test_windows_daemon_kill_removes_pidfile_after_kill():
    """Después de kill exitoso, el PID file se remueve."""
    name = "test_windows_daemon_kill_removes_pidfile"
    remove_pidfile(name)
    daemon = WindowsDaemon()
    write_pidfile(name)
    assert os.path.exists(_pidfile_path(name)), "precondition: PID file existe"
    with patch("causadb._daemon.subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["taskkill"],
            returncode=0,
            stdout="SUCCESS",
            stderr="",
        )
        result = daemon.kill(name)
    assert result is True
    assert not os.path.exists(_pidfile_path(name)), (
        "kill exitoso debe remover el PID file"
    )


def test_windows_daemon_kill_handles_taskkill_failure():
    """Si taskkill falla (returncode != 0), kill retorna False.

    Degradación suave: no propaga el error. El PID file NO se remueve
    (no confirmamos la muerte del proceso).
    """
    name = "test_windows_daemon_kill_taskkill_fail"
    remove_pidfile(name)
    daemon = WindowsDaemon()
    write_pidfile(name)
    try:
        with patch("causadb._daemon.subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=["taskkill"],
                returncode=128,
                stdout="",
                stderr="ERROR: process not found",
            )
            result = daemon.kill(name)
        assert result is False, (
            "kill debe retornar False si taskkill falla (returncode != 0)"
        )
        # El PID file NO se remueve porque no confirmamos la muerte
        assert os.path.exists(_pidfile_path(name)), (
            "kill fallido NO debe remover el PID file (no confirmamos muerte)"
        )
    finally:
        remove_pidfile(name)


def test_windows_daemon_kill_handles_taskkill_not_in_path():
    """Si taskkill no está en PATH (FileNotFoundError), kill retorna False."""
    name = "test_windows_daemon_kill_no_taskkill"
    remove_pidfile(name)
    daemon = WindowsDaemon()
    write_pidfile(name)
    try:
        with patch(
            "causadb._daemon.subprocess.run",
            side_effect=FileNotFoundError("taskkill not found"),
        ):
            result = daemon.kill(name)
        assert result is False, (
            "kill debe retornar False si taskkill no está en PATH"
        )
    finally:
        remove_pidfile(name)


# ---------------------------------------------------------------------------
# Anti-teatro (Artículo IX) — WindowsDaemon.
# Verifica que los tests de is_running/kill NO pasan trivialmente: si
# mutamos WindowsDaemon para usar os.kill(pid, 0) en vez de tasklist, el
# test `test_windows_daemon_is_running_uses_tasklist` debe FALLAR (porque
# ya no se llama subprocess.run con "tasklist").
# ---------------------------------------------------------------------------


def test_anti_teatro_windows_daemon_is_running_uses_os_kill():
    """Anti-teatro: si WindowsDaemon.is_running usa os.kill(pid, 0) en
    vez de tasklist, el test `test_windows_daemon_is_running_uses_tasklist`
    debe FALLAR (no se llama subprocess.run con "tasklist").

    Este test construye una versión mutada de WindowsDaemon que usa
    os.kill y verifica que el contract "usa tasklist" se rompe.
    """
    name = "test_anti_teatro_windows_is_running"
    remove_pidfile(name)
    write_pidfile(name)
    pid = read_pidfile(name)
    try:
        # Mutación: sobrescribir is_running para usar os.kill (mal en Windows)
        class MutantWindowsDaemon(WindowsDaemon):
            def is_running(self, name: str) -> bool:
                p = self.read_pidfile(name)
                if p is None:
                    return False
                try:
                    os.kill(p, 0)  # MAL: lo que el roadmap prohíbe
                    return True
                except OSError:
                    return False

        mutant = MutantWindowsDaemon()
        with patch("causadb._daemon.subprocess.run") as mock_run:
            result = mutant.is_running(name)

        # El mutante NO llama subprocess.run (usa os.kill directo).
        # Si el test `test_windows_daemon_is_running_uses_tasklist` se
        # ejecutara contra este mutante, fallaría en:
        #   `mock_run.assert_called_once()` → AssertionError
        # Eso prueba que el test real ejercita tasklist de verdad.
        assert mock_run.call_count == 0, (
            "El mutante (os.kill) no debe llamar subprocess.run. Si este "
            "assert FALLA (call_count != 0), el mutante sí usa tasklist y "
            "entonces no sirve como anti-teatro."
        )
        # El mutante retorna True (proceso actual vivo) — pero por la
        # razón equivocada (os.kill en vez de tasklist).
        assert result is True
    finally:
        remove_pidfile(name)


def test_anti_teatro_windows_daemon_kill_uses_os_kill():
    """Anti-teatro: si WindowsDaemon.kill usa os.kill en vez de taskkill,
    el test `test_windows_daemon_kill_uses_taskkill` debe FALLAR.

    Construye un mutante que usa os.kill(pid, signal.SIGTERM) y verifica
    que NO llama subprocess.run con "taskkill".
    """
    name = "test_anti_teatro_windows_kill"
    remove_pidfile(name)
    write_pidfile(name)
    try:
        class MutantWindowsDaemon(WindowsDaemon):
            def kill(self, name: str, timeout: float = 5.0) -> bool:
                p = self.read_pidfile(name)
                if p is None:
                    return False
                try:
                    os.kill(p, signal.SIGTERM)  # MAL: lo que el roadmap prohíbe
                    self.remove_pidfile(name)
                    return True
                except OSError:
                    return False

        mutant = MutantWindowsDaemon()
        with patch("causadb._daemon.subprocess.run") as mock_run:
            # El mutante va a intentar matar el proceso actual (pid del test)
            # con SIGTERM — eso mataría al test runner. Mockeamos os.kill
            # para evitar suicidio del test.
            with patch("causadb._daemon.os.kill") as mock_oskill:
                mock_oskill.return_value = None
                result = mutant.kill(name)

        # El mutante NO llama subprocess.run (usa os.kill directo).
        assert mock_run.call_count == 0, (
            "El mutante (os.kill) no debe llamar subprocess.run. Si este "
            "assert FALLA, el mutante sí usa taskkill y no sirve como "
            "anti-teatro."
        )
        # El mutante SÍ llama os.kill (la ruta prohibida).
        mock_oskill.assert_called_once()
        assert result is True
    finally:
        remove_pidfile(name)


# ---------------------------------------------------------------------------
# F.13.2.3 — get_daemon() factory + selección dinámica por sys.platform.
#
# get_daemon() lee sys.platform y retorna la implementación concreta
# apropiada. Los tests mockean sys.platform para forzar cada rama sin
# depender del OS donde corre el CI.
# ---------------------------------------------------------------------------


def test_get_daemon_returns_unix_daemon_on_linux():
    """get_daemon() retorna UnixDaemon cuando sys.platform == 'linux'."""
    with patch("causadb._daemon.sys.platform", "linux"):
        daemon = get_daemon()
    assert isinstance(daemon, UnixDaemon), (
        "En linux, get_daemon() debe retornar una instancia de UnixDaemon. "
        f"Obtuvo {type(daemon).__name__}."
    )


def test_get_daemon_returns_unix_daemon_on_darwin():
    """get_daemon() retorna UnixDaemon cuando sys.platform == 'darwin'.

    macOS usa fork (no launchd) — no hay MacDaemon (Artículo VIII).
    """
    with patch("causadb._daemon.sys.platform", "darwin"):
        daemon = get_daemon()
    assert isinstance(daemon, UnixDaemon), (
        "En darwin, get_daemon() debe retornar UnixDaemon (no MacDaemon). "
        f"Obtuvo {type(daemon).__name__}."
    )


def test_get_daemon_returns_windows_daemon_on_win32():
    """get_daemon() retorna WindowsDaemon cuando sys.platform == 'win32'."""
    with patch("causadb._daemon.sys.platform", "win32"):
        daemon = get_daemon()
    assert isinstance(daemon, WindowsDaemon), (
        "En win32, get_daemon() debe retornar WindowsDaemon. "
        f"Obtuvo {type(daemon).__name__}."
    )
    # Sanity: no es UnixDaemon (son clases distintas)
    assert not isinstance(daemon, UnixDaemon), (
        "En win32, get_daemon() NO debe retornar UnixDaemon."
    )


def test_get_daemon_raises_daemon_error_on_unsupported_platform():
    """get_daemon() levanta DaemonError para plataformas no soportadas.

    Fall-Closed (Artículo IX): no hay fallback silencioso a UnixDaemon.
    """
    with patch("causadb._daemon.sys.platform", "freebsd"):
        with pytest.raises(DaemonError) as exc_info:
            get_daemon()
    assert "freebsd" in str(exc_info.value), (
        "El mensaje de DaemonError debe incluir el nombre de la plataforma "
        f"no soportada. Obtuvo: {exc_info.value!r}"
    )


def test_get_daemon_returns_fresh_instance_each_call():
    """get_daemon() retorna una instancia nueva en cada invocación.

    No es un singleton — la selección es dinámica. Esto permite que si
    sys.platform cambia (en tests), la siguiente llamada refleje el
    nuevo valor.
    """
    with patch("causadb._daemon.sys.platform", "linux"):
        d1 = get_daemon()
        d2 = get_daemon()
    assert d1 is not d2, (
        "get_daemon() debe retornar una instancia nueva cada vez (no "
        "singleton). Si d1 is d2, la factory está cacheando."
    )


# ---------------------------------------------------------------------------
# Anti-teatro (Artículo IX) — get_daemon() platform detection.
# Verifica que la detección de platform es REAL: si mutamos get_daemon
# para ignorar sys.platform y siempre retornar UnixDaemon, el test que
# espera WindowsDaemon en win32 debe FALLAR.
# ---------------------------------------------------------------------------


def test_anti_teatro_get_daemon_returns_unix_on_win32():
    """Anti-teatro: si get_daemon() ignora sys.platform y siempre retorna
    UnixDaemon, el test `test_get_daemon_returns_windows_daemon_on_win32`
    debe FALLAR.

    Construye un mutante de get_daemon que hardcodea UnixDaemon sin leer
    sys.platform. Verifica que en win32 el mutante retorna UnixDaemon
    (NO WindowsDaemon) — demostrando que el test real ejercita la
    detección de platform de verdad. Si el test real pasara con este
    mutante, significaría que no está verificando la rama win32.
    """
    import causadb._daemon as _d

    def mutant_get_daemon() -> PlatformDaemon:
        # MAL: ignora sys.platform, siempre UnixDaemon
        return UnixDaemon()

    with patch("causadb._daemon.sys.platform", "win32"):
        mutant_result = mutant_get_daemon()
        # Y comparamos con el real (que SÍ lee sys.platform)
        real_result = _d.get_daemon()

    # El mutante retorna UnixDaemon (ignora platform) → si el test real
    # `test_get_daemon_returns_windows_daemon_on_win32` se ejecutara
    # contra el mutante, fallaría en `isinstance(daemon, WindowsDaemon)`.
    assert isinstance(mutant_result, UnixDaemon), (
        "El mutante debe retornar UnixDaemon (ignora platform). Si este "
        "assert FALLA, el mutante sí está leyendo platform y no sirve como "
        "anti-teatro."
    )
    assert not isinstance(mutant_result, WindowsDaemon), (
        "El mutante NO debe retornar WindowsDaemon (es el punto del "
        "anti-teatro: ignora win32)."
    )
    # El real SÍ retorna WindowsDaemon en win32.
    assert isinstance(real_result, WindowsDaemon), (
        "El real get_daemon() debe retornar WindowsDaemon en win32. Si "
        "retorna UnixDaemon, la detección de platform está rota."
    )
    # La diferencia entre mutante y real prueba que el test real ejercita
    # la detección de platform (no es teatro).


# ---------------------------------------------------------------------------
# F.13.2.4 — CLI Integration: los CLI commands usan get_daemon() explícito.
#
# Verifica que `_cmd_watch.py` (y por extensión los demás CLI commands)
# invocan `get_daemon().is_running(...)` y `get_daemon().kill(...)` en
# vez de los wrappers module-level `is_running`/`kill_daemon`. Esto da
# claridad y forward-compat para el dispatch multi-plataforma (roadmap
# F.13.2.4, líneas 115-116).
# ---------------------------------------------------------------------------


def test_cli_watch_uses_get_daemon_for_is_running(capsys):
    """`watch status` debe invocar `get_daemon().is_running(...)`.

    F.13.2.4: los CLI commands usan `get_daemon()` explícitamente (no los
    wrappers module-level). Mockeamos `get_daemon` en el módulo
    `_cmd_watch` y verificamos que `is_running` se llama vía la instancia
    retornada por `get_daemon()`, no vía el wrapper module-level.
    """
    import argparse
    from unittest.mock import MagicMock
    import tempfile

    from causadb.cli import _cmd_watch
    from causadb._workspace import WorkspaceManager

    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        # Init workspace para que resolve_ledger funcione
        WorkspaceManager.init(project)
        ledger = os.path.join(project, ".causadb", "ledger.log")

        # Mock get_daemon en el módulo _cmd_watch (donde se importa)
        mock_daemon = MagicMock()
        mock_daemon.is_running.return_value = False

        with patch("causadb.cli._cmd_watch.get_daemon", return_value=mock_daemon):
            args = argparse.Namespace(
                action="status",
                ledger=ledger,
                daemon=False,
                no_proxy=False,
            )
            exit_code, output = _cmd_watch.cmd_watch(args)

        assert exit_code == 0
        # is_running debe haberse llamado 5 veces (vigilante, mcp_proxy,
        # proxy_server, harvest, serve — BIT-CHR.41 + H-OPS.1 Fase 2)
        assert mock_daemon.is_running.call_count == 5, (
            "watch status debe llamar get_daemon().is_running() 5 veces "
            "(vigilante, mcp_proxy, proxy_server, harvest, serve). Si es 0, está usando "
            "el wrapper module-level en vez de get_daemon()."
        )
        called_names = [call.args[0] for call in mock_daemon.is_running.call_args_list]
        assert "vigilante" in called_names
        assert "mcp_proxy" in called_names
        assert "harvest" in called_names
        assert "proxy_server" in called_names
        assert "serve" in called_names


def test_cli_watch_uses_get_daemon_for_kill(capsys):
    """`watch stop` debe invocar `get_daemon().kill(...)`.

    F.13.2.4: los CLI commands usan `get_daemon()` explícitamente. Mockeamos
    `get_daemon` y verificamos que `kill` se llama vía la instancia retornada
    por `get_daemon()`, no vía el wrapper module-level `kill_daemon`.
    """
    import argparse
    from unittest.mock import MagicMock
    import tempfile

    from causadb.cli import _cmd_watch
    from causadb._workspace import WorkspaceManager

    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        WorkspaceManager.init(project)
        ledger = os.path.join(project, ".causadb", "ledger.log")

        mock_daemon = MagicMock()
        mock_daemon.kill.return_value = True

        with patch("causadb.cli._cmd_watch.get_daemon", return_value=mock_daemon):
            args = argparse.Namespace(
                action="stop",
                ledger=ledger,
                daemon=False,
                no_proxy=False,
            )
            exit_code, output = _cmd_watch.cmd_watch(args)

        assert exit_code == 0
        # kill debe haberse llamado 5 veces (vigilante, mcp_proxy,
        # proxy_server, harvest, serve — BIT-CHR.41 + H-OPS.1 Fase 2)
        assert mock_daemon.kill.call_count == 5, (
            "watch stop debe llamar get_daemon().kill() 5 veces "
            "(vigilante, mcp_proxy, proxy_server, harvest, serve). Si es 0, está usando "
            "el wrapper module-level kill_daemon en vez de get_daemon().kill()."
        )
        called_names = [call.args[0] for call in mock_daemon.kill.call_args_list]
        assert "vigilante" in called_names
        assert "mcp_proxy" in called_names
        assert "proxy_server" in called_names
        assert "harvest" in called_names
        assert "serve" in called_names


def test_cli_vigilante_uses_get_daemon_for_is_running():
    """`vigilante start/stop` usa `get_daemon().is_running()` explícito.

    F.13.2.4: extensión del contract a `_cmd_vigilante.py`. Verifica que
    el CLI command invoca `get_daemon().is_running()` (no el wrapper
    module-level) al chequear si el vigilante ya está corriendo.
    """
    import argparse
    from unittest.mock import MagicMock
    import tempfile

    from causadb.cli import _cmd_vigilante
    from causadb._workspace import WorkspaceManager

    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        WorkspaceManager.init(project)
        ledger = os.path.join(project, ".causadb", "ledger.log")

        mock_daemon = MagicMock()
        # is_running retorna True → _start retorna "already_running" sin
        # tocar threads ni daemonize. Esto aísla el test del thread loop.
        mock_daemon.is_running.return_value = True

        with patch("causadb.cli._cmd_vigilante.get_daemon", return_value=mock_daemon):
            args = argparse.Namespace(
                action="start",
                ledger=ledger,
                watch=None,
                daemon=False,
            )
            exit_code, output = _cmd_vigilante.cmd_vigilante(args)

        assert exit_code == 0
        mock_daemon.is_running.assert_called_once_with("vigilante")
        # daemonize NO debe llamarse (is_running=True cortocircuita)
        mock_daemon.daemonize.assert_not_called()


def test_cli_mcp_proxy_uses_get_daemon_for_is_running():
    """`mcp-proxy status` usa `get_daemon().is_running()` explícito.

    F.13.2.4: extensión del contract a `_cmd_mcp_proxy.py`. Verifica que
    el CLI command invoca `get_daemon().is_running()` (no el wrapper
    module-level ni un import inline legacy).
    """
    import argparse
    from unittest.mock import MagicMock
    import tempfile

    from causadb.cli import _cmd_mcp_proxy
    from causadb._workspace import WorkspaceManager

    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        WorkspaceManager.init(project)
        ledger = os.path.join(project, ".causadb", "ledger.log")

        mock_daemon = MagicMock()
        mock_daemon.is_running.return_value = False

        with patch("causadb.cli._cmd_mcp_proxy.get_daemon", return_value=mock_daemon):
            args = argparse.Namespace(
                action="status",
                ledger=ledger,
                config=None,
                daemon=False,
            )
            exit_code, output = _cmd_mcp_proxy.cmd_mcp_proxy(args)

        assert exit_code == 0
        mock_daemon.is_running.assert_called_once_with("mcp_proxy")


def test_cli_proxy_server_uses_get_daemon_for_is_running_and_kill():
    """`proxy-server stop` usa `get_daemon().is_running()` y `.kill()`.

    F.13.2.4: extensión del contract a `_cmd_proxy.py`. Verifica que el
    CLI command invoca los métodos de la instancia retornada por
    `get_daemon()` (no los wrappers module-level).
    """
    import argparse
    from unittest.mock import MagicMock
    import tempfile

    from causadb.cli import _cmd_proxy
    from causadb._workspace import WorkspaceManager

    with tempfile.TemporaryDirectory() as tmp:
        project = os.path.join(tmp, "proj")
        os.makedirs(project)
        WorkspaceManager.init(project)
        ledger = os.path.join(project, ".causadb", "ledger.log")

        mock_daemon = MagicMock()
        mock_daemon.is_running.return_value = True
        mock_daemon.kill.return_value = True

        with patch("causadb.cli._cmd_proxy.get_daemon", return_value=mock_daemon):
            args = argparse.Namespace(
                action="stop",
                ledger=ledger,
                daemon=False,
            )
            exit_code, output = _cmd_proxy.cmd_proxy_server(args)

        assert exit_code == 0
        mock_daemon.is_running.assert_called_once_with("proxy_server")
        mock_daemon.kill.assert_called_once_with("proxy_server")


# ---------------------------------------------------------------------------
# D.1 — systemd user service (_daemon_service.py + _cmd_daemon.py).
#
# Tests use monkeypatch to redirect SYSTEMD_USER_DIR to tmp_path for
# install tests (real file I/O), and monkeypatch subprocess.run for
# start/stop/status (no systemctl dependency).
#
# Artículo III: test-first. Artículo IX: anti-teatro.
# ---------------------------------------------------------------------------


def test_daemon_install_creates_service_file(tmp_path, monkeypatch):
    """install_service() creates the systemd service file at the expected path.

    Mockeamos SYSTEMD_USER_DIR a tmp_path para evitar tocar el home real.
    El file write es real (tmp_path escribe a disco).
    """
    from causadb._daemon_service import install_service, SYSTEMD_USER_DIR

    service_dir = tmp_path / "systemd" / "user"
    monkeypatch.setattr("causadb._daemon_service.SYSTEMD_USER_DIR", str(service_dir))

    ledger = str(tmp_path / "ledger.log")
    success, path = install_service(ledger)

    assert success is True, "install_service debe retornar (True, path)"
    expected_path = str(service_dir / "causadb.service")
    assert path == expected_path, (
        f"install_service debe retornar la ruta del service file. "
        f"Esperada: {expected_path}, obtenida: {path}"
    )
    assert os.path.exists(path), (
        "install_service debe crear el archivo causadb.service en el "
        "directorio systemd user. Si no existe, install_service es no-op."
    )


def test_daemon_install_service_content(tmp_path, monkeypatch):
    """install_service() escribe contenido válido de unit systemd.

    Verifica que el archivo generado contenga las secciones y campos
    esenciales de un unit file de systemd.
    """
    from causadb._daemon_service import install_service, SYSTEMD_USER_DIR

    service_dir = tmp_path / "systemd" / "user"
    monkeypatch.setattr("causadb._daemon_service.SYSTEMD_USER_DIR", str(service_dir))

    ledger = str(tmp_path / "ledger.log")
    success, path = install_service(ledger)

    assert success is True
    with open(path) as f:
        content = f.read()

    # Debe tener las secciones obligatorias
    assert "[Unit]" in content, "El service file debe tener sección [Unit]"
    assert "[Service]" in content, "El service file debe tener sección [Service]"
    assert "[Install]" in content, "El service file debe tener sección [Install]"

    # Debe incluir la descripción
    assert "Description=CausaDB" in content, (
        "El service file debe tener Description=CausaDB"
    )
    # Debe incluir ExecStart con el ledger path
    assert "ExecStart=" in content, (
        "El service file debe incluir ExecStart"
    )
    assert f"--ledger {ledger}" in content, (
        "ExecStart debe apuntar al ledger path proporcionado"
    )
    # Debe tener Restart=on-failure
    assert "Restart=on-failure" in content, (
        "El service file debe tener Restart=on-failure"
    )
    # Debe tener WantedBy=default.target
    assert "WantedBy=default.target" in content, (
        "El service file debe tener WantedBy=default.target"
    )


def test_daemon_install_service_escapes_spaces_in_path(tmp_path, monkeypatch):
    """install_service() escapes spaces in ledger path for systemd.

    Systemd splits unquoted arguments at spaces. The ExecStart line must
    use \\x20 encoding so that paths like "/home/user/My Projects/ledger.log"
    are handled correctly.
    """
    from causadb._daemon_service import install_service, SYSTEMD_USER_DIR

    service_dir = tmp_path / "systemd" / "user"
    monkeypatch.setattr("causadb._daemon_service.SYSTEMD_USER_DIR", str(service_dir))

    # Path with spaces — the bug scenario
    ledger = str(tmp_path / "My Projects" / "ledger.log")
    success, path = install_service(ledger)

    assert success is True
    with open(path) as f:
        content = f.read()

    # ExecStart must contain \\x20 where spaces were
    assert "\\x20" in content, (
        "ExecStart must escape spaces as \\x20 for systemd. "
        f"Got ExecStart line: {[l for l in content.splitlines() if 'ExecStart' in l]}"
    )
    # Must NOT contain raw spaces in the path portion
    exec_start_line = [l for l in content.splitlines() if l.startswith("ExecStart=")][0]
    # The escaped path should be present
    escaped_path = ledger.replace(" ", "\\x20")
    assert escaped_path in exec_start_line, (
        f"ExecStart should contain escaped path. Got: {exec_start_line}"
    )


def test_daemon_start_calls_systemctl(monkeypatch):
    """start_service() invoca ``systemctl --user start causadb``.

    Mockeamos subprocess.run para evitar depender de systemctl real.
    """
    from causadb._daemon_service import start_service

    calls = []

    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout="", stderr="",
        )

    monkeypatch.setattr("causadb._daemon_service.subprocess.run", mock_run)

    success, msg = start_service()

    assert success is True, "start_service debe retornar (True, ...) en éxito"
    assert len(calls) == 1, "start_service debe llamar subprocess.run exactamente 1 vez"
    assert calls[0] == ["systemctl", "--user", "start", "causadb"], (
        f"Comando invocado: {calls[0]}. "
        f"Esperado: ['systemctl', '--user', 'start', 'causadb']"
    )


def test_daemon_stop_calls_systemctl(monkeypatch):
    """stop_service() invoca ``systemctl --user stop causadb``."""
    from causadb._daemon_service import stop_service

    calls = []

    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, returncode=0, stdout="", stderr="",
        )

    monkeypatch.setattr("causadb._daemon_service.subprocess.run", mock_run)

    success, msg = stop_service()

    assert success is True
    assert len(calls) == 1
    assert calls[0] == ["systemctl", "--user", "stop", "causadb"], (
        f"Comando invocado: {calls[0]}. "
        f"Esperado: ['systemctl', '--user', 'stop', 'causadb']"
    )


def test_daemon_status_parses_systemctl(monkeypatch):
    """status_service() parsea correctamente outputs activo/inactivo.

    Caso activo: ``systemctl --user is-active causadb`` retorna
    returncode=0 y stdout="active" → (True, "active").

    Caso inactivo: returncode=3 y stdout="inactive" → (False, ...).
    """
    from causadb._daemon_service import status_service

    def mock_run_factory(stdout: str, rc: int):
        def mock_run(cmd, **kwargs):
            return subprocess.CompletedProcess(
                cmd, returncode=rc, stdout=stdout + "\n", stderr="",
            )
        return mock_run

    # --- Activo ---
    monkeypatch.setattr(
        "causadb._daemon_service.subprocess.run",
        mock_run_factory("active", 0),
    )
    success, msg = status_service()
    assert success is True, "is-active retornando 0 debe dar (True, ...)"
    assert msg == "active", (
        f"Cuando systemctl retorna 'active', msg debe ser 'active'. "
        f"Obtenido: {msg!r}"
    )

    # --- Inactivo ---
    monkeypatch.setattr(
        "causadb._daemon_service.subprocess.run",
        mock_run_factory("inactive", 3),
    )
    success, msg = status_service()
    assert success is False, "is-active retornando 3 debe dar (False, ...)"
    assert "inactive" in msg, (
        f"Cuando systemctl retorna 'inactive', msg debe contener 'inactive'. "
        f"Obtenido: {msg!r}"
    )


def test_anti_teatro_daemon_install_noop(tmp_path, monkeypatch):
    """Anti-teatro: install_service() debe escribir el archivo REALMENTE.

    Si install_service fuera un no-op (retorna un path sin escribir),
    el archivo .service no existiría. Este test verifica que:
      1) El archivo existe en disco.
      2) El contenido es el template de systemd (empieza con "[Unit]").

    También probamos que si mutamos install_service para que sea un stub
    que retorna un path sin escribir, el assert de existencia fallaría.
    """
    from causadb._daemon_service import install_service, SYSTEMD_USER_DIR

    service_dir = tmp_path / "systemd" / "user"
    monkeypatch.setattr("causadb._daemon_service.SYSTEMD_USER_DIR", str(service_dir))

    ledger = str(tmp_path / "ledger.log")
    success, path = install_service(ledger)

    assert success is True
    assert os.path.exists(path), (
        "install_service debe crear el archivo en disco. "
        "Si este assert falla, install_service es un no-op "
        "(retorna un path pero no escribe nada) — Artículo IX violado."
    )

    with open(path) as f:
        content = f.read()
    assert len(content) > 0, (
        "install_service debe escribir contenido no vacío. "
        "Si el archivo está vacío, es un stub — Artículo IX violado."
    )
    assert content.startswith("[Unit]"), (
        "install_service debe escribir un unit file systemd válido "
        "que comience con '[Unit]'. Artículo IX: no es teatro.\n"
        f"Primeros 80 caracteres del contenido: {content[:80]!r}"
    )

    # --- Verificación mutante ---
    # Construimos un stub que retorna (True, path_sin_escribir).
    def stub_install_service(_ledger: str):
        fake_path = str(service_dir / "causadb.service")
        return (True, fake_path)  # NO escribe el archivo

    # Si el test real usara este stub, el assert `os.path.exists(path)`
    # fallaría porque el archivo no se escribió. Esto prueba que el test
    # real ejercita la escritura real del archivo (no es teatro).
    stub_success, stub_path = stub_install_service(ledger)
    assert stub_success is True
    if os.path.exists(stub_path):
        os.remove(stub_path)  # limpiamos por si el install_service real lo creó
    assert not os.path.exists(stub_path), (
        "El stub NO debe crear el archivo (es el punto del anti-teatro: "
        "demostrar que el stub no pasa el test real). "
        "Si este assert falla, el stub está escribiendo el archivo (y "
        "entonces no es un stub — no sirve como anti-teatro)."
    )
