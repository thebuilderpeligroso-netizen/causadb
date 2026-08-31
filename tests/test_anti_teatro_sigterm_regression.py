"""Regression determinista para el flake anti-teatro (BIT-CHR.N+1).

Reproduce el bug del test
``test_daemon_serve_wiring.py::test_anti_teatro_sigterm_without_handler_kills_daemon``
de forma DETERMINISTA: simula el estado global pytest que deja
``test_serve_start_daemon_port_in_use_emits_hint`` (un handler SIGTERM
instalado en el proceso pytest padre que NUNCA se restaura) y luego corre
el mismo escenario anti-teatro (fork + child SIN handler + SIGTERM).

Sin Fix B (``signal.signal(signal.SIGTERM, signal.SIG_DFL)`` dentro del
child), el hijo hereda el handler global y SIGTERM lo hace salir con
exit 0 → ``WIFEXITED=True`` → assertion falla.

Con Fix B, el child restaura SIGTERM al default ANTES de entrar al loop
y muere por la señal (``WIFSIGNALED=True``, ``WIFEXITED=False``).

Anti-teatro: el test captura muerte por señal real (fork + os.kill +
waitpid + WIFEXITED/WIFSIGNALED), no un mockeo trivial. Si alguien
rompe el threading contract del daemon con un handler real NUEVO
instalado dentro del child, este restore no lo protege — el test
``test_sigterm_stops_registered_harvest_daemon`` sigue siendo el
discriminador de ese contrato.
"""

import os
import signal
import time


def test_anti_teatro_sigterm_kills_daemon_even_with_global_handler(tmp_path):
    """Regression: si un test previo deja un handler SIGTERM global
    instalado en el proceso pytest (p.ej. por patch mal apuntado a
    ``causadb._daemon_service.install_signal_handlers`` en lugar de
    ``causadb.cli._cmd_serve.install_signal_handlers``), el test
    anti-teatro del módulo ``test_daemon_serve_wiring`` todavía debe
    matar el hijo por la señal y no con exit 0."""
    from causadb._daemon_service import (
        HarvesterDaemon,
        install_signal_handlers,
        set_current_harvester_daemon,
    )

    # Instalar un handler SIGTERM "espurio" en el proceso pytest,
    # simulando lo que deja test_serve_start_daemon_port_in_use_emits_hint
    # cuando el patch no intercepta la llamada real.
    install_signal_handlers(str(tmp_path / "dummy.log"))
    try:
        ledger_path = str(tmp_path / "ledger.log")
        pid = os.fork()
        if pid == 0:
            # ---- Child SIN handlers (mutante) ----
            # FIX B: restaurar SIGTERM al default aunque el pytest padre
            # tenga un handler global instalado por otro test (p.ej.
            # test_serve_start_daemon_port_in_use_emits_hint parcheando
            # _daemon_service pero no _cmd_serve). Sin esto, el hijo
            # hereda el handler y SIGTERM sale con exit 0.
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            daemon = HarvesterDaemon(ledger_path=ledger_path)
            set_current_harvester_daemon(daemon)
            while True:
                time.sleep(1)
        else:
            try:
                time.sleep(1.0)
                os.kill(pid, signal.SIGTERM)
                _, status = os.waitpid(pid, 0)
                assert not os.WIFEXITED(status), (
                    f"Hijo debe morir por señal, no exit 0; status={status!r}"
                )
            finally:
                try:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                except (OSError, ChildProcessError):
                    pass
    finally:
        # Restore SIGTERM al default en el proceso pytest para no
        # contaminar tests siguientes (defensa idiomática).
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
