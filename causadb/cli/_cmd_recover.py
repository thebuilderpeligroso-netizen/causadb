"""`causadb recover` subcommand — recovery de sesiones (Fase 13).

Capa 4 del círculo de contexto: dado un ``session_id`` (o un ``--search``
keyword) recupera el detalle completo de la sesión desde la FUENTE CRUDA
de la herramienta, componiendo el storyboard con ``build_storyboard``
(DRY con la Fase 12).

Artículo II: thin wrapper — toda la lógica vive en ``_recover_session``.
"""

import json
from typing import Tuple

from causadb._recover_session import (
    AmbiguousSessionError,
    SessionNotFoundError,
    recover_session,
    search_stories,
)
from causadb._workspace import resolve_ledger, NoWorkspaceError


def cmd_recover(args) -> Tuple[int, str]:
    """Route ``causadb recover <session_id> [--tool X]`` / ``--search <kw>``."""
    try:
        ledger = resolve_ledger(args.ledger)
    except NoWorkspaceError as e:
        return (1, json.dumps({"error": str(e)}))

    if getattr(args, "search", None):
        return _search(ledger, args)

    if not args.session_id:
        return (1, json.dumps({"error": "session_id required (or --search <keyword>)."}))

    # C.4 — Si el session_id tiene un conversation_ref en el ledger, lo usa
    # para resolver el provider sin recorrer fuentes. Si no, degrada al
    # mecanismo actual (recorrido de fuentes). Producto-nativo: el operador
    # pasa el session_id que vio en la tarjeta de revive, y recover hace el
    # lookup solo.
    conversation_ref = None
    explicit_tool = getattr(args, "tool", None)
    if not explicit_tool:
        try:
            from causadb._replay_engine import ReplayEngine
            state = ReplayEngine(ledger).reconstruct_state()
            convs = state.get("conversations_recoverable", {})
            if args.session_id in convs:
                conversation_ref = convs[args.session_id].get("conversation_ref")
        except Exception:
            conversation_ref = None  # degrade gracefully

    try:
        tool, storyboard = recover_session(
            ledger, args.session_id,
            tool=explicit_tool,
            conversation_ref=conversation_ref,
        )
    except SessionNotFoundError as e:
        return (1, json.dumps({"error": str(e)}))
    except AmbiguousSessionError as e:
        return (1, json.dumps({"error": str(e)}))

    return (0, json.dumps({"tool": tool, "storyboard": storyboard}, indent=2))


def _search(ledger: str, args) -> Tuple[int, str]:
    try:
        matches = search_stories(
            ledger, args.search, tool=getattr(args, "tool", None)
        )
    except Exception as e:  # defensivo: search no debe romper la CLI
        return (1, json.dumps({"error": str(e)}))
    return (0, json.dumps({"matches": matches}, indent=2))
