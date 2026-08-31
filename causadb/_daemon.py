"""Platform daemon lifecycle — ABC + Unix/Windows implementations.

Artículo VII: mínimo funcional. PID-based lifecycle, no systemd.
Artículo I: este módulo NO escribe al ledger. Solo gestiona procesos.
Artículo VIII: una sola abstracción (PlatformDaemon) con implementaciones
concretas. UnixDaemon (F.13.2.1) y WindowsDaemon (F.13.2.2).
No se crea MacDaemon (Artículo VIII — sin abstracciones sin implementaciones).

F.13.2.1: refactor de funciones module-level a ABC + UnixDaemon.
F.13.2.2: WindowsDaemon — subprocess DETACHED_PROCESS + tasklist/taskkill.
F.13.2.3: `get_daemon()` factory que selecciona la implementación por
`sys.platform`. Las funciones module-level (`daemonize`, `is_running`,
`kill_daemon`) delegan ahora a `get_daemon()` (selección dinámica, no
singleton hardcodeado). Backward compatibility preservada para los
callers existentes (`_cmd_watch.py`, `_cmd_proxy.py`,
`_cmd_vigilante.py`, `_cmd_mcp_proxy.py`).
"""

import os
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from typing import Optional


PID_DIR = os.path.expanduser("~/.causadb/pids")
LOG_DIR = os.path.expanduser("~/.causadb/logs")


class DaemonError(Exception):
    """Excepción común para errores de lifecycle del daemon.

    Artículo IX: Fall-Closed. Los errores de fork/kill no son silenciosos.
    """


def _ensure_dirs():
    os.makedirs(PID_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def _pidfile_path(name: str) -> str:
    _ensure_dirs()
    return os.path.join(PID_DIR, f"{name}.pid")


def _logfile_path(name: str) -> str:
    _ensure_dirs()
    return os.path.join(LOG_DIR, f"{name}.log")


# ---------------------------------------------------------------------------
# Cross-platform PID file helpers (module-level, usados por la ABC y por
# los callers legacy directamente). Quedan como funciones porque no tienen
# comportamiento polimórfico — son puramente I/O de archivos.
# ---------------------------------------------------------------------------


def write_pidfile(name: str) -> str:
    path = _pidfile_path(name)
    with open(path, "w") as f:
        f.write(str(os.getpid()))
        f.flush()
        os.fsync(f.fileno())
    return path


def read_pidfile(name: str) -> Optional[int]:
    path = _pidfile_path(name)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        try:
            return int(f.read().strip())
        except (ValueError, OSError):
            return None


def remove_pidfile(name: str) -> None:
    path = _pidfile_path(name)
    if os.path.exists(path):
        os.remove(path)


# ---------------------------------------------------------------------------
# PlatformDaemon ABC (F.13.2.1)
# ---------------------------------------------------------------------------


class PlatformDaemon(ABC):
    """Abstract base class para lifecycle de daemons multi-plataforma.

    Artículo VIII: una abstracción solo si tiene múltiples implementaciones
    concretas (o camino claro a ellas). UnixDaemon existe ahora;
    WindowsDaemon viene en F.13.2.2.

    Métodos abstractos (polimórficos por plataforma):
        - daemonize(name, logfile): fork/setsid/redirect en Unix;
          equivalente Windows en F.13.2.2.
        - is_running(name): chequeo de proceso vivo.
        - kill(name, timeout): terminación graceful + force.

    Métodos concretos (cross-platform, I/O de archivos):
        - write_pidfile, read_pidfile, remove_pidfile: delegan a las
          funciones module-level existentes (no son polimórficos).
    """

    @abstractmethod
    def daemonize(self, name: str, logfile: Optional[str] = None) -> None:
        """Detach del proceso controlador y escribir PID file.

        Raises:
            DaemonError: si la plataforma no soporta daemonización o
                falla el fork/redirect.
        """

    @abstractmethod
    def is_running(self, name: str) -> bool:
        """True si el proceso daemonizado con `name` está vivo."""

    @abstractmethod
    def kill(self, name: str, timeout: float = 5.0) -> bool:
        """Terminar el daemon. SIGTERM → wait → SIGKILL si hace falta.

        Returns:
            True si el proceso fue terminado (o ya estaba muerto al
            intentar señalarlo y se limpió el PID file), False si no
            había PID file.
        """

    # -- Cross-platform PID file I/O (delegan a funciones module-level) --

    def write_pidfile(self, name: str) -> str:
        return write_pidfile(name)

    def read_pidfile(self, name: str) -> Optional[int]:
        return read_pidfile(name)

    def remove_pidfile(self, name: str) -> None:
        remove_pidfile(name)


# ---------------------------------------------------------------------------
# UnixDaemon — implementación concreta para Linux/Unix (double-fork).
# Contiene el código que antes vivía en las funciones module-level.
# ---------------------------------------------------------------------------


class UnixDaemon(PlatformDaemon):
    """Daemon Unix vía double-fork + setsid.

    Este es el comportamiento que históricamente implementaban las
    funciones module-level `daemonize`, `is_running`, `kill_daemon`.
    """

    def daemonize(self, name: str, logfile: Optional[str] = None) -> None:
        """Fork, setsid, redirect stdio, write PID file.

        The child process continues execution from the caller's code after
        this function returns. The parent process exits immediately.

        Raises:
            OSError: if fork fails (Artículo IX: Fall-Closed, no silent
                failures).
        """
        try:
            pid = os.fork()
        except OSError:
            raise
        if pid > 0:
            os._exit(0)
        os.setsid()
        try:
            pid2 = os.fork()
        except OSError:
            raise
        if pid2 > 0:
            os._exit(0)
        log_path = logfile or _logfile_path(name)
        with open(log_path, "a") as log:
            os.dup2(log.fileno(), 0)
            os.dup2(log.fileno(), 1)
            os.dup2(log.fileno(), 2)
        os.chdir("/")
        write_pidfile(name)

    def is_running(self, name: str) -> bool:
        pid = read_pidfile(name)
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def kill(self, name: str, timeout: float = 5.0) -> bool:
        pid = read_pidfile(name)
        if pid is None:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
            waited = 0.0
            interval = 0.1
            while waited < timeout:
                try:
                    os.kill(pid, 0)
                except OSError:
                    remove_pidfile(name)
                    return True
                time.sleep(interval)
                waited += interval
            os.kill(pid, signal.SIGKILL)
            remove_pidfile(name)
            return True
        except OSError:
            remove_pidfile(name)
            return False


# ---------------------------------------------------------------------------
# WindowsDaemon — implementación concreta para Windows (F.13.2.2).
#
# Windows no tiene fork(2). La daemonización se modela distinto:
#   - Enfoque A (adoptado): el caller YA es el proceso que correrá como
#     daemon. `daemonize()` solo (a) escribe el PID file con `os.getpid()`
#     y (b) abre el logfile en append. No redirige stdio (Windows no
#     soporta dup2 a un log file de la misma forma que Unix). El caller
#     es responsable de mantenerse vivo.
#   - Enfoque B (descartado): re-executar el script con
#     `subprocess.Popen` + `DETACHED_PROCESS` y un flag "soy el worker".
#     Más fiel al modelo Unix pero específico al caller y complejo.
#
# `is_running` usa `tasklist` (NO `os.kill(pid, 0)` — en Windows levanta
# OSError también por falta de permisos, no solo por proceso inexistente).
# `kill` usa `taskkill /PID <pid> /F` como método primario.
# ---------------------------------------------------------------------------


class WindowsDaemon(PlatformDaemon):
    """Windows daemon — subprocess DETACHED_PROCESS + tasklist/taskkill.

    F.13.2.2 del roadmap. Windows no tiene fork; `daemonize` adopta el
    Enfoque A: el caller es el daemon, `daemonize` solo escribe el PID
    file (con `os.getpid()`) y abre el logfile en append.

    `is_running` consulta `tasklist /FI "PID eq <pid>"` y verifica que el
    PID aparezca en el output. `kill` invoca `taskkill /PID <pid> /F`.

    Nota: `tasklist`/`taskkill` son built-ins de Windows. En Linux no
    existen — los tests mockean `subprocess.run` para simular su
    comportamiento. La degradación suave (returncode != 0) retorna False
    sin propagar errores (Artículo IX: Fall-Closed en kill, pero no
    crash del caller).
    """

    def daemonize(self, name: str, logfile: Optional[str] = None) -> None:
        """Escribir PID file con el PID del caller y abrir logfile.

        Enfoque A: el caller ya es el proceso daemon. No hay fork en
        Windows. No se redirige stdio (Windows no soporta dup2 a un
        log file de la misma forma que Unix); el caller es responsable
        de loggear al `logfile` si lo desea.

        Raises:
            DaemonError: si falla la escritura del PID file.
        """
        try:
            write_pidfile(name)
        except OSError as exc:
            raise DaemonError(
                f"WindowsDaemon: no se pudo escribir PID file para '{name}': {exc}"
            ) from exc
        # Abrir el logfile en append para crearlo/tocarlo. No redirigimos
        # stdio: el caller decide cómo loggear. Abrirlo aquí asegura que
        # el archivo exista y sea appendable (mismo contrato que UnixDaemon
        # en cuanto a que el logfile queda creado).
        log_path = logfile or _logfile_path(name)
        try:
            with open(log_path, "a"):
                pass  # touch (create if not exists, preserve content)
        except OSError as exc:
            raise DaemonError(
                f"WindowsDaemon: no se pudo abrir logfile '{log_path}': {exc}"
            ) from exc

    def is_running(self, name: str) -> bool:
        """True si el proceso con el PID del PID file está vivo.

        Usa `tasklist /FI "PID eq <pid>"` (NO `os.kill(pid, 0)` — en
        Windows ese último levanta OSError también por falta de permisos,
        no solo por proceso inexistente; ver roadmap F.13.2.2).

        Degradación suave: si `tasklist` falla (returncode != 0, no
        encontrado en PATH, etc.), retorna False sin propagar el error.
        """
        pid = self.read_pidfile(name)
        if pid is None:
            return False
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, FileNotFoundError):
            # tasklist no está en PATH (ej. corriendo en Linux sin mock)
            # o falló el spawn. Degradación suave.
            return False
        if result.returncode != 0:
            return False
        # tasklist filtra por PID pero el output puede incluir el header
        # "INFO: No tasks are running which match the specified criteria."
        # Verificamos que el PID aparezca como token en el output.
        return str(pid) in (result.stdout or "")

    def kill(self, name: str, timeout: float = 5.0) -> bool:
        """Terminar el daemon vía `taskkill /PID <pid> /F`.

        Método primario: `taskkill /PID <pid> /F` (force kill).
        No hay equivalente de SIGTERM graceful en este nivel; `taskkill`
        sin `/F` enviaría WM_CLOSE a procesos con ventana, pero los
        daemons de CausaDB son headless → `/F` directo.

        Returns:
            True si `taskkill` reporta éxito (returncode == 0) y el PID
            file se remueve. False si no hay PID file o `taskkill` falla.

        `timeout` se acepta por contract de la ABC pero no se usa en
        Windows (no hay polling de SIGTERM→SIGKILL; `/F` es síncrono).
        """
        pid = self.read_pidfile(name)
        if pid is None:
            return False
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, FileNotFoundError):
            # taskkill no está en PATH o falló el spawn. No propagamos
            # el error (degradación suave). El PID file queda (no
            # confirmamos la muerte).
            return False
        if result.returncode != 0:
            # taskkill falló (proceso inexistente, permisos, etc.).
            # Degradación suave: retornamos False sin propagar.
            return False
        self.remove_pidfile(name)
        return True


# ---------------------------------------------------------------------------
# Platform factory + thin wrappers module-level (backward compatibility).
#
# Los callers existentes hacen `from causadb._daemon import daemonize, ...`
# y deben seguir funcionando sin cambios. Estos wrappers delegan a
# `get_daemon()`, que selecciona la implementación concreta según
# `sys.platform` en cada invocación (selección dinámica, no singleton
# hardcodeado — F.13.2.3).
# ---------------------------------------------------------------------------


def get_daemon() -> PlatformDaemon:
    """Return the platform-appropriate daemon implementation.

    Selección dinámica basada en `sys.platform`:

    - ``linux``   → :class:`UnixDaemon`
    - ``darwin``  → :class:`UnixDaemon` (fork funciona en macOS moderno;
      no se introduce ``MacDaemon`` hasta que haya evidencia real de
      necesidad de launchd — Artículo VIII: sin abstracciones sin
      implementaciones concretas que las justifiquen).
    - ``win32``   → :class:`WindowsDaemon`
    - otro        → raise :class:`DaemonError` (Fall-Closed, Artículo IX).

    Returns:
        Una instancia nueva de la implementación concreta apropiada para
        la plataforma actual.

    Raises:
        DaemonError: si la plataforma no está soportada.
    """
    platform = sys.platform
    if platform == "linux" or platform == "darwin":
        return UnixDaemon()
    elif platform == "win32":
        return WindowsDaemon()
    else:
        raise DaemonError(f"Unsupported platform: {platform}")


def daemonize(name: str, logfile: Optional[str] = None) -> None:
    """Thin wrapper que delega a ``get_daemon().daemonize``.

    Backward compat: preserva el contrato de la función module-level
    original. La implementación concreta se selecciona dinámicamente
    según ``sys.platform`` (F.13.2.3).
    """
    return get_daemon().daemonize(name, logfile)


def is_running(name: str) -> bool:
    """Thin wrapper que delega a ``get_daemon().is_running``."""
    return get_daemon().is_running(name)


def kill_daemon(name: str, timeout: float = 5.0) -> bool:
    """Thin wrapper que delega a ``get_daemon().kill``.

    El nombre module-level se preserva (``kill_daemon``, no ``kill``) para
    no romper callers existentes.
    """
    return get_daemon().kill(name, timeout)
