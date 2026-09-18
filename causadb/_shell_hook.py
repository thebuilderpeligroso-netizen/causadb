"""Shell command hook — bash trap DEBUG + PROMPT_COMMAND auto-logging.

Captures every shell command via ``trap DEBUG`` / ``PROMPT_COMMAND`` and
writes them to a queue file. ``flush()`` reads the queue and persists each
command as a ``COMMAND_RUN`` event to the CausaDB ledger.

Artículo I: flush() uses LedgerWriter.append(). Never opens ledger.log directly.
Artículo VII: mínimo funcional. Bash-only hot path (no Python in trap/PROMPT).
"""

import base64
import json
import os
import re
from typing import Optional

from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._schema_validator import validate_event_schema


# Lazy-resolved paths (call-time, not import-time) so monkeypatched HOME
# is reflected. Module-level constants would freeze ~ at import time.
def _hook_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".causadb", "shell_hook")

def _hook_script() -> str:
    return os.path.join(_hook_dir(), "hook.sh")

def _queue_file() -> str:
    return os.path.join(_hook_dir(), "queue.jsonl")


HOOK_TEMPLATE = """# === CausaDB Shell Hook ===
__causadb_queue="{queue_path}"
__causadb_ctx_id={ctx_id}

# Save any previous PROMPT_COMMAND so our hook can call it
__causadb_prev_prompt="${{PROMPT_COMMAND:-}}"

__causadb_hook_log() {{
    [ -n "$__causadb_saved_cmd" ] || return
    local ec=$?
    local cmd_b64="$(printf '%s' "$__causadb_saved_cmd" | base64 -w0)"
    printf '{{"event_type":"COMMAND_RUN","source":"shell:bash","source_type":"agent","ctx_id":"%s","payload":{{"command_b64":"%s","exit_code":%d}}}}\\n' \
        "$__causadb_ctx_id" \
        "$cmd_b64" \
        "$ec" >> "$__causadb_queue"
}}
trap '[[ $BASH_COMMAND != __causadb_hook_log* ]] && __causadb_saved_cmd=$BASH_COMMAND' DEBUG

# Compose PROMPT_COMMAND with the previous one WITHOUT eval (avoids
# double-eval of $(...) / quotes; keeps starship/direnv intact).
# bash >= 5.1: single-element array -> each entry runs as its own command.
# Older bash: fallback to ';' concatenation.
if [[ -n "$__causadb_prev_prompt" ]]; then
    if (( BASH_VERSINFO[0] > 5 || (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] >= 1) )); then
        PROMPT_COMMAND=( "$__causadb_prev_prompt" __causadb_hook_log )
    else
        PROMPT_COMMAND="$__causadb_prev_prompt; __causadb_hook_log"
    fi
else
    PROMPT_COMMAND='__causadb_hook_log'
fi
"""


def install(ctx_id: str = "shell") -> bool:
    """Install the shell hook.

    Creates ~/.causadb/shell_hook/ and writes hook.sh.
    Appends ``source ~/.causadb/shell_hook/hook.sh`` to ``~/.bashrc`` if not
    already present.

    Args:
        ctx_id: Context ID to stamp on events (default "shell").
            Only alphanumeric, hyphens, and underscores allowed.

    Returns:
        True if hook was installed, False if already present.

    Raises:
        ValueError: if ctx_id contains invalid characters.
    """
    if not re.match(r"^[a-zA-Z0-9_-]+$", ctx_id):
        raise ValueError(
            f"ctx_id must be alphanumeric (hyphens/underscores OK): {ctx_id!r}"
        )

    os.makedirs(_hook_dir(), exist_ok=True)
    os.chmod(_hook_dir(), 0o700)

    # Pre-create the queue file with restrictive perms (0o600) so the hook
    # never depends on umask. os.open mode is subject to umask, so chmod
    # afterwards to guarantee 0600 regardless of the caller's umask.
    fd = os.open(_queue_file(), os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(_queue_file(), 0o600)

    # Escape ctx_id with json.dumps to prevent shell injection in the template
    safe_ctx = json.dumps(ctx_id)
    content = HOOK_TEMPLATE.format(queue_path=_queue_file(), ctx_id=safe_ctx)
    with open(_hook_script(), "w") as f:
        f.write(content)
    os.chmod(_hook_script(), 0o755)

    # Check ~/.bashrc
    bashrc = os.path.expanduser("~/.bashrc")
    source_line = f"source {_hook_script()}"
    if os.path.exists(bashrc):
        with open(bashrc) as f:
            if source_line in f.read():
                return False  # already installed

    with open(bashrc, "a") as f:
        f.write(f"\n# CausaDB Shell Hook\n{source_line}\n")
    return True


def remove() -> bool:
    """Remove the shell hook from ~/.bashrc.

    Returns:
        True if line was removed, False if not found.
    """
    bashrc = os.path.expanduser("~/.bashrc")
    source_line = f"source {_hook_script()}"
    if not os.path.exists(bashrc):
        return False

    with open(bashrc) as f:
        lines = f.readlines()

    new_lines = [l for l in lines if source_line not in l and "# CausaDB Shell Hook" not in l]
    if len(new_lines) == len(lines):
        return False

    with open(bashrc, "w") as f:
        f.writelines(new_lines)
    return True


def status() -> dict:
    """Check if the shell hook is installed.

    Returns:
        dict with keys: installed (bool), hook_path (str), queue_path (str).
    """
    bashrc = os.path.expanduser("~/.bashrc")
    source_line = f"source {_hook_script()}"
    installed = False
    if os.path.exists(bashrc):
        with open(bashrc) as f:
            installed = source_line in f.read()
    return {
        "installed": installed,
        "hook_path": _hook_script(),
        "queue_path": _queue_file(),
    }


def flush(ledger_path: str) -> dict:
    """Flush the command queue to the CausaDB ledger.

    Reads the queue file atomically (rename -> process -> remove), validates
    each line, and writes valid COMMAND_RUN events to the ledger.

    Args:
        ledger_path: Absolute path to the ledger file.

    Returns:
        dict with keys: flushed (int), errors (int).
    """
    if not os.path.exists(_queue_file()):
        return {"flushed": 0, "errors": 0}

    # Atomic rename to avoid race condition with concurrent bash writes
    tmp_queue = _queue_file() + ".flushing"
    try:
        os.rename(_queue_file(), tmp_queue)
    except OSError:
        return {"flushed": 0, "errors": 0, "error": "cannot lock queue"}

    from causadb._ledger_writer import LedgerWriter

    writer = LedgerWriter(ledger_path)
    flushed = 0
    errors = 0

    try:
        with open(tmp_queue, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    raw_payload = data.get("payload", {})
                    # Fase 1: el hook nuevo manda command_b64 (robusto ante
                    # comillas/backslash/newlines). Tolerar formato viejo
                    # (command en claro) para no perder la cola pendiente.
                    raw_cmd = raw_payload.get("command", "")
                    b64 = raw_payload.get("command_b64")
                    if b64:
                        try:
                            raw_cmd = base64.b64decode(b64).decode("utf-8", errors="replace")
                        except Exception:
                            pass
                    event = CanonicalEvent(
                        event_type=EventType.COMMAND_RUN,
                        ctx_id=data.get("ctx_id", "shell"),
                        source=data.get("source", "shell:bash"),
                        source_type=data.get("source_type", "agent"),
                        payload={
                            "command": raw_cmd,
                            "exit_code": raw_payload.get("exit_code", -1),
                        },
                    )
                    # La redacción de secretos dentro de `command` la aplica
                    # LedgerWriter.append (redact recursivo + regex alta
                    # precisión) en el camino de persistencia.
                    # Validate schema before writing (Artículo I)
                    vr = validate_event_schema(event)
                    if not vr.is_valid:
                        errors += 1
                        continue
                    writer.append(event)
                    flushed += 1
                except Exception:
                    errors += 1
    finally:
        if os.path.exists(tmp_queue):
            os.remove(tmp_queue)

    return {"flushed": flushed, "errors": errors}
