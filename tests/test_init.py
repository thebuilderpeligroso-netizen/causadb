import pytest
import os
import json
import sys
import io
import shutil
from pathlib import Path
from causadb._init import causadb_init
from causadb._config import CausaDBConfig
from causadb.cli._cmd_init import cmd_init
from causadb._workspace import WorkspaceManager

def test_init_creates_ledger_file(tmp_path):
    path = str(tmp_path / "workspace")
    causadb_init(path)
    assert os.path.exists(os.path.join(path, "ledger.log"))

def test_init_creates_genesis_event(tmp_path):
    """Valida todos los campos del GENESIS según el plan P.9."""
    path = str(tmp_path / "workspace")
    causadb_init(path)
    with open(os.path.join(path, "ledger.log"), "r") as f:
        entry = json.loads(f.readline())
    ev = entry["event"]
    assert ev["event_type"] == "SYSTEM_BOOT"
    assert ev["ctx_id"] == "genesis"
    assert ev["source"] == "causadb:init", f"FAIL source={ev['source']}"
    assert ev["source_type"] == "human", f"FAIL source_type={ev['source_type']}"
    assert ev["parent_event_id"] is None, (
        f"FAIL parent_event_id={ev['parent_event_id']} — debe ser None"
    )
    assert ev["payload"] == {"action": "init"}
    assert ev["metadata"]["trace_id"] == "init"
    assert ev["metadata"]["session_id"] == "init"

def test_init_creates_chronicle(tmp_path):
    """Article IX: chronicle debe tener header válido, no solo existir."""
    path = str(tmp_path / "workspace")
    causadb_init(path)
    chronicle = os.path.join(path, "CAUSADB_CHRONICLE.md")
    assert os.path.exists(chronicle)
    with open(chronicle) as f:
        content = f.read()
    assert "CAUSADB_CHRONICLE" in content, (
        f"Chronicle debe tener header con 'CAUSADB_CHRONICLE'. Got: {content[:100]}"
    )
    assert len(content) > 0, "Chronicle no debe estar vacío"


def test_init_creates_shared_docs(tmp_path):
    """Init crea anotadores de coordinación multi-agente en .causadb/coordination/."""
    path = str(tmp_path / "workspace")
    WorkspaceManager.init(path)

    audit_path = os.path.join(path, ".causadb", "coordination", "AUDIT_REPORT.json")
    action_path = os.path.join(path, ".causadb", "coordination", "ACTION_PLAN.json")

    assert os.path.exists(audit_path), "AUDIT_REPORT.json no creado"
    assert os.path.exists(action_path), "ACTION_PLAN.json no creado"

    with open(audit_path) as f:
        audit = json.load(f)
    assert audit["tipo"] == "AUDIT_REPORT"
    assert audit["estado"] == "BORRADOR"
    assert audit["version"] == 1

    with open(action_path) as f:
        action = json.load(f)
    assert action["tipo"] == "ACTION_PLAN"
    assert action["solicitud_al_auditor"] == "APROBAR / OBJETAR"
    assert action["version"] == 1


def test_init_idempot(tmp_path):
    path = str(tmp_path / "workspace")
    causadb_init(path)
    with pytest.raises(FileExistsError):
        causadb_init(path)

def test_init_relative_path_raises():
    """Article IX + Bloqueante #7: causadb_init debe Fall-Closed en su
    propia validación de path absoluto — no delegado a LedgerWriter."""
    with pytest.raises(ValueError, match="absolute"):
        causadb_init("relative")


# ---------------------------------------------------------------------------
# BIT-14.7 — init sin argumentos (default CWD)
# ---------------------------------------------------------------------------

def test_cmd_init_no_workspace_defaults_to_cwd(tmp_path, monkeypatch):
    """`causadb init` sin workspace explícito usa CWD.
    
    Anti-teatro: un stub que falla si no recibe workspace posicional
    rompería. Verificamos que .causadb/ se crea en tmp_path.
    """
    from causadb.cli.main import main
    monkeypatch.chdir(tmp_path)
    rc = main(["init"])
    # Success — .causadb/ creado en tmp_path
    assert rc == 0, f"exit code {rc}"
    assert (tmp_path / ".causadb" / "ledger.log").exists(), (
        "ledger.log no creado en CWD"
    )
    assert (tmp_path / ".causadb" / "config.json").exists(), (
        "config.json no creado en CWD"
    )


def test_cmd_init_no_workspace_prints_path(tmp_path, monkeypatch, capsys):
    """`causadb init` imprime la ruta del workspace creado.
    
    Anti-teatro: un stub que no imprime nada rompería la aserción
    de que el JSON de salida contiene config_path apuntando a CWD.
    """
    from causadb.cli.main import main
    monkeypatch.chdir(tmp_path)
    rc = main(["init"])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert tmp_path == tmp_path  # sanity
    assert str(tmp_path / ".causadb" / "config.json") in payload.get("config_path", ""), (
        f"config_path no apunta a CWD: {payload}"
    )


def test_cmd_init_agent_configures_mcp(tmp_path, monkeypatch):
    """`init --agent codex` also writes the Codex MCP configuration."""
    from causadb.cli.main import main

    monkeypatch.chdir(tmp_path)
    rc = main(["init", "--agent", "codex", "--telemetry-enabled", "false"])
    assert rc == 0

    output = tmp_path / ".codex" / "config.toml"
    assert output.exists()
    import tomllib
    with output.open("rb") as f:
        config = tomllib.load(f)
    entry = config["mcp_servers"]["causadb"]
    assert entry["command"].endswith("causadb-mcp")
    assert entry["args"] == []
    assert entry["env"]["CAUSADB_LEDGER_PATH"] == str(
        tmp_path / ".causadb" / "ledger.log"
    )


def test_cmd_init_cursor_configures_mcp(tmp_path, monkeypatch):
    """`init --agent cursor` writes the Cursor project MCP config."""
    from causadb.cli.main import main

    monkeypatch.chdir(tmp_path)
    rc = main(["init", "--agent", "cursor", "--telemetry-enabled", "false"])
    assert rc == 0

    output = tmp_path / ".cursor" / "mcp.json"
    assert output.exists()
    with output.open() as f:
        config = json.load(f)
    entry = config["mcpServers"]["causadb"]
    assert entry["command"].endswith("causadb-mcp")
    assert entry["args"] == []
    assert entry["env"]["CAUSADB_LEDGER_PATH"] == str(
        tmp_path / ".causadb" / "ledger.log"
    )


def test_cmd_init_windsurf_configures_mcp(tmp_path, monkeypatch):
    from causadb.cli.main import main
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "home" / p.replace("~/", "")))
    assert main(["init", "--agent", "windsurf", "--telemetry-enabled", "false"]) == 0
    assert (tmp_path / "home" / ".codeium" / "windsurf" / "mcp_config.json").exists()


@pytest.mark.parametrize("agent", ["grok", "hermes", "openjarvis", "devin"])
def test_cmd_init_out_of_scope_agent_does_not_create_mcp(tmp_path, monkeypatch, agent):
    from causadb.cli.main import main
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--agent", agent, "--telemetry-enabled", "false"]) == 0
    assert not list(tmp_path.glob("**/*mcp*"))


def test_cmd_init_existing_workspace_agent_configures_mcp(
    tmp_path, monkeypatch, capsys, fake_home
):
    """Connecting to an existing workspace applies explicit MCP setup.

    Uso fake_home: `--agent gemini` dispara seed_doctrina_link, que escribe
    en `Path.home()`. Sin aislar el home, el test corrompe el GEMINI.md real
    del operador con un ledger_path stale a un tempdir de pytest (regresión
    del WARN-1, hallado por el Checker 2026-08-14).
    """
    from causadb.cli.main import main

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert main(["init", str(workspace), "--telemetry-enabled", "false"]) == 0
    capsys.readouterr()

    child = workspace / "child"
    child.mkdir()
    monkeypatch.setattr("sys.stdin", io.StringIO("Y\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    rc = main([
        "init", str(child), "--agent", "gemini", "--telemetry-enabled", "false"
    ])
    assert rc == 0
    output = capsys.readouterr().out
    payload = json.loads(output[output.find("{"):])
    assert payload["mcp_tool"] == "gemini-cli"
    assert (workspace / ".gemini" / "settings.json").exists()


def test_cmd_init_with_hook_calls_install(tmp_path, monkeypatch):
    """`causadb init --with-hook` llama a shell_hook.install() con ctx_id correcto.
    
    Anti-teatro: un stub de cmd_init que ignora --with-hook falla.
    """
    from causadb.cli.main import main
    from causadb._shell_hook import install as real_install

    install_calls = []

    def spy_install(ctx_id="shell"):
        install_calls.append(ctx_id)
        return True

    monkeypatch.setattr("causadb.cli._cmd_init.install_shell_hook", spy_install)
    monkeypatch.chdir(tmp_path)

    rc = main(["init", "--with-hook"])
    assert rc == 0, f"exit code {rc}"

    assert len(install_calls) == 1, (
        f"install_shell_hook() no fue llamado exactamente una vez: "
        f"{len(install_calls)}"
    )
    # ctx_id debe derivarse del nombre del directorio (no el default "shell")
    project_name = os.path.basename(tmp_path)
    expected_ctx = f"init_{project_name}"
    assert install_calls[0] == expected_ctx, (
        f"ctx_id debe ser {expected_ctx!r}, got {install_calls[0]!r}"
    )


def test_cmd_init_with_hook_preserves_explicit_workspace(tmp_path, monkeypatch):
    """`causadb init /path --with-hook` usa el path explícito, no CWD.
    
    Anti-teatro: un stub que ignora el path explícito y usa CWD falla.
    """
    from causadb.cli.main import main

    install_calls = []

    def spy_install(ctx_id="shell"):
        install_calls.append(ctx_id)
        return True

    monkeypatch.setattr("causadb.cli._cmd_init.install_shell_hook", spy_install)
    monkeypatch.chdir(tmp_path)

    explicit = tmp_path / "my_project"
    rc = main(["init", str(explicit), "--with-hook"])
    assert rc == 0, f"exit code {rc}"

    # Workspace creado en explicit, no en tmp_path
    assert (explicit / ".causadb" / "ledger.log").exists()
    assert not (tmp_path / ".causadb" / "ledger.log").exists()

    # ctx_id usa el nombre del path explícito
    assert install_calls[0] == "init_my_project", (
        f"expected init_my_project, got {install_calls[0]!r}"
    )


# ---------------------------------------------------------------------------
# GAP 5 — Fase G5.B (init agnóstico): detectar workspace existente en ancestro
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Aísla Path.home() a un tmp para que detect/seed NUNCA toquen el home real.

    WARN-1: el test G5.B original escribió el marker stale en
    ~/.config/opencode/AGENTS.md del operador — el home real.
    """
    home = tmp_path / "fake_home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    monkeypatch.setattr("shutil.which", lambda _: None)
    return home


def _g5b_args(**overrides):
    base = {"workspace": None, "git_hooks": False, "with_hook": False,
            "with_assistant": False, "telemetry_enabled": None,
            "no_seed_doctrina": False, "agent": None}
    base.update(overrides)
    return type("Args", (), base)()


def test_cmd_init_detects_existing_workspace_in_ancestor(tmp_path, fake_home, monkeypatch):
    """RED: si un ancestro tiene .causadb/config.json, cmd_init NO crea duplicado."""
    ancestor = tmp_path / "proyecto"
    ancestor.mkdir()
    sub_dir = ancestor / "sub"
    sub_dir.mkdir()

    WorkspaceManager.init(str(ancestor))

    monkeypatch.setattr("sys.stdin", io.StringIO("Y\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    args = _g5b_args(workspace=str(sub_dir))
    exit_code, output_json = cmd_init(args)

    assert exit_code == 0
    assert not (sub_dir / ".causadb").exists(), "init creó duplicado en subdirectorio"
    assert "connected" in output_json


def test_cmd_init_n_aborts_no_duplicate(tmp_path, fake_home, monkeypatch):
    """RED: si el usuario responde 'n', init aborta y NO crea duplicado."""
    ancestor = tmp_path / "proyecto"
    ancestor.mkdir()
    sub_dir = ancestor / "sub"
    sub_dir.mkdir()

    WorkspaceManager.init(str(ancestor))

    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    args = _g5b_args(workspace=str(sub_dir))
    exit_code, output_json = cmd_init(args)

    assert exit_code == 1, f"'n' debe abortar con exit 1. Got {exit_code}"
    assert not (sub_dir / ".causadb").exists(), "no debe haber creado duplicado"


def test_cmd_init_no_ancestor_creates_new_workspace(tmp_path, fake_home, monkeypatch):
    """RED: si no hay ancestro con workspace, init hace lo de siempre (backward-compat)."""
    fresh_dir = tmp_path / "fresh"
    fresh_dir.mkdir()

    monkeypatch.setattr("sys.stdin", io.StringIO("Y\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    args = _g5b_args(workspace=str(fresh_dir))
    exit_code, output_json = cmd_init(args)

    assert exit_code == 0
    assert (fresh_dir / ".causadb").exists(), "debe crear workspace nuevo si no hay ancestro"


def test_cmd_init_no_seed_doctrina_flag_skips_writing(fake_home, monkeypatch):
    """WARN-3: con --no-seed-doctrina, connect-mode NO escribe en archivos de reglas."""
    fresh_dir = fake_home / "ws"
    fresh_dir.mkdir()
    WorkspaceManager.init(str(fresh_dir))

    opencode_dir = fake_home / ".config" / "opencode"
    opencode_dir.mkdir(parents=True)
    ag = opencode_dir / "AGENTS.md"
    ag.write_text("# reglas existentes\n")

    monkeypatch.setattr("sys.stdin", io.StringIO("Y\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    args = _g5b_args(workspace=str(fresh_dir), no_seed_doctrina=True)
    exit_code, output_json = cmd_init(args)

    assert exit_code == 0
    assert ag.read_text() == "# reglas existentes\n", "no debe haberse sembrado doctrina"


def test_cmd_init_connect_warns_on_ignored_create_flags(fake_home, monkeypatch):
    """WARN-2: en connect-mode, flags de 'crear' generan warnings en el JSON."""
    ancestor = fake_home / "p"
    ancestor.mkdir()
    WorkspaceManager.init(str(ancestor))

    monkeypatch.setattr("sys.stdin", io.StringIO("Y\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    args = _g5b_args(workspace=str(ancestor), git_hooks=True, with_hook=True,
                     with_assistant=True)
    exit_code, output_json = cmd_init(args)

    assert exit_code == 0
    out = json.loads(output_json)
    assert len(out["warnings"]) == 3
    assert any("--git-hooks" in w for w in out["warnings"])
    assert any("--with-hook" in w for w in out["warnings"])
    assert any("--with-assistant" in w for w in out["warnings"])


def test_seed_doctrina_link_updates_stale_marker(fake_home):
    """WARN-1: seed REEMPLAZA el bloque si el marker ya existe con path stale."""
    rules = fake_home / ".config" / "opencode" / "AGENTS.md"
    rules.parent.mkdir(parents=True)
    stale = ("# reglas\n\n<!-- CAUSADB-GAP5 -->\n"
             "CausaDB ledger_path=/tmp/pytest-of-xxx/pytest-376/stale\n"
             "Doctrina de uso: ver docs/canon.md\n"
             "<!-- CAUSADB-GAP5 --><!-- end -->\n\nmás contenido\n")
    rules.write_text(stale)

    new_path = str(fake_home / ".causadb" / "ledger.log")
    from causadb.cli._cmd_init import seed_doctrina_link
    returned = seed_doctrina_link("opencode", new_path)

    assert returned == str(rules)
    content = rules.read_text()
    assert "/tmp/pytest-of-xxx/pytest-376/stale" not in content
    assert new_path in content
    assert content.count("CAUSADB-GAP5") == 2, "un solo bloque (open+close)"


def test_seed_doctrina_link_creates_file_when_missing(fake_home):
    """seed crea el archivo de reglas si no existe."""
    from causadb.cli._cmd_init import seed_doctrina_link
    new_path = str(fake_home / ".causadb" / "ledger.log")
    returned = seed_doctrina_link("opencode", new_path)

    assert returned == str(fake_home / ".config" / "opencode" / "AGENTS.md")
    content = (fake_home / ".config" / "opencode" / "AGENTS.md").read_text()
    assert new_path in content
    assert content.count("CAUSADB-GAP5") == 2


def test_seed_doctrina_link_pointer_is_agnostic(fake_home):
    """El puntero sembrado es agnóstico: sin URLs externas ni rutas de máquina.

    Doctrina (BIT-49 / briefing:92): el canon vive DENTRO del producto. El
    puntero NO puede apuntar a una URL de GitHub (el repo no existe) ni a
    una ruta absoluta del desarrollador (rompe en cualquier otra máquina).
    En cambio apunta a los casos de uso + el mecanismo producto-nativo
    (`causadb canon` / resource MCP causadb://canon).

    Anti-teatro: este test fallaría si alguien reintroduce una URL o una
    ruta absoluta en el bloque — ambos son errores ya cometidos.
    """
    from causadb.cli._cmd_init import seed_doctrina_link
    new_path = str(fake_home / ".causadb" / "ledger.log")
    returned = seed_doctrina_link("opencode", new_path)

    content = (fake_home / ".config" / "opencode" / "AGENTS.md").read_text()

    # 1. Sin URLs externas (el repo de GitHub todavía no existe).
    assert "github.com" not in content, (
        "el puntero no debe depender de una URL externa inexistente"
    )
    assert "http://" not in content and "https://" not in content, (
        "el puntero no debe depender de URLs externas"
    )

    # 2. Sin ruta absoluta de archivo apuntando al canon (el único path
    #    permitido es el ledger_path del workspace, agnóstico por definición).
    #    Anti-teatro: si alguien reintroduce una ruta tipo
    #    /home/juliussb/.../docs/canon.md, el bloque contiene un path .md.
    assert "canon.md" not in content, (
        "el puntero no debe contener un path de archivo al canon (vive en el paquete)"
    )
    assert "docs/canon" not in content, (
        "el puntero no debe contener una ruta a docs/canon.md"
    )

    # 3. Apunta a casos de uso + mecanismo producto-nativo.
    assert "causadb canon" in content or "causadb://canon" in content, (
        "el puntero debe indicar cómo leer el canon (CLI o resource MCP)"
    )
    # 4. Incluye los casos de uso (para que el agente sepa cuándo leerlo).
    assert any(word in content.lower() for word in (
        "auditar", "reconstruir", "recuperar", "trazabilidad", "decisiones"
    )), "el puntero debe listar casos de uso"

    # 5. El ledger_path del workspace SÍ viaja (correcto, es agnóstico).
    assert new_path in content


def test_seed_doctrina_link_pointer_refers_to_canon(fake_home):
    """El puntero refiere al canon como documento aparte, no lo infla.

    Doctrina: el canon se mantiene una sola vez; el puntero NO duplica su
    contenido. El bloque debe ser corto y no enumerar las tools (viven en
    el canon, no en el puntero).
    """
    from causadb.cli._cmd_init import seed_doctrina_link
    new_path = str(fake_home / ".causadb" / "ledger.log")
    seed_doctrina_link("opencode", new_path)

    content = (fake_home / ".config" / "opencode" / "AGENTS.md").read_text()
    # El bloque no debe listar las tools (causadb_query, causadb_revive, ...)
    # — eso infla el puntero y duplica el canon.
    assert "causadb_query" not in content, (
        "el puntero no debe enumerar tools (viven en el canon)"
    )
    # El bloque debe ser corto (pointer, no canon embebido).
    block = content.split("<!-- CAUSADB-GAP5 -->")[1].split(
        "<!-- CAUSADB-GAP5 --><!-- end -->")[0]
    assert len(block.splitlines()) <= 14, (
        f"el puntero debe ser corto, no inflado: {len(block.splitlines())} líneas"
    )


def test_seed_doctrina_link_pointer_carries_regla1(fake_home):
    """La REGLA 1 (cierre de sesión) viaja en el puntero, no solo en el canon.

    Decisión del operador (2026-08-14): la REGLA 1 es comportamiento
    proactivo que depende 100% del agente — el agente puede no llamar
    revive en una sesión, pero el archivo de reglas se lee SIEMPRE al
    arrancar. Por eso la regla vive en el puntero (único lugar de lectura
    garantizada), no en el canon (bajo demanda).
    """
    from causadb.cli._cmd_init import seed_doctrina_link
    new_path = str(fake_home / ".causadb" / "ledger.log")
    seed_doctrina_link("opencode", new_path)

    content = (fake_home / ".config" / "opencode" / "AGENTS.md").read_text()
    assert "REGLA 1" in content, (
        "el puntero debe incluir la REGLA 1 (cierre de sesión)"
    )
    assert "GOVERNANCE_DECISION" in content, (
        "el puntero debe indicar loguear una GOVERNANCE_DECISION al cerrar"
    )
    assert "proxima sesión" in content or "próxima sesión" in content, (
        "el puntero debe explicar por qué (recuperar contexto barato)"
    )


def test_detect_agent_empty_home_returns_none(fake_home):
    """detect_agent: sin archivos de reglas ni binarios → None."""
    from causadb.cli._cmd_init import detect_agent
    assert detect_agent() is None


# ---------------------------------------------------------------------------
# Deuda #23 — desambiguación multi-agente en detect_agent / cmd_init.
#
# Causa raíz: ``detect_agent()`` preciede por archivo de reglas existente en
# orden hardcodeado [opencode, gemini, claude, codex]. En una máquina con
# ambos ``AGENTS.md`` y ``GEMINI.md`` instalados, opencode SIEMPRE gana
# aunque quien haya tirado ``causadb init`` sea gemini-cli. La semilla del
# puntero a canon.md cae al archivo equivocado.
#
# Fix: ``detect_agent(explicit=None)`` acepta override ``--agent``; en su
# defecto cae a env ``CAUSADB_AGENT``; último recurso la heurística. El flag
# ``--agent`` del CLI es el único camino robusto en multi-agente.
# ---------------------------------------------------------------------------

def test_detect_agent_explicit_override_beats_heuristic(fake_home):
    """--agent gemini gana incluso cuando AGENTS.md existe (escenario del bug).

    Anti-teatro: sin el override este test pasaría ``detect_agent() is None``
    trivialmente (fake_home vacío). Acá forzamos un ``AGENTS.md`` y
    ``GEMINI.md`` existentes — la heurística devolvería ``"opencode"`` y el
    override debe imponerse.
    """
    from causadb.cli._cmd_init import detect_agent, _agent_rules_map

    # Crear ambos archivos — la heurística "natural" devolvería opencode.
    rules = _agent_rules_map(fake_home)
    for path in rules.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# reglas\n")

    # Heurística sola → opencode (el primero en el hardcode).
    assert detect_agent() == "opencode"
    # Override explícito → gana.
    assert detect_agent("gemini") == "gemini"


def test_detect_agent_env_var_beats_heuristic(fake_home, monkeypatch):
    """CAUSADB_AGENT=gemini gana sobre la heurística cuando no hay --agent."""
    from causadb.cli._cmd_init import detect_agent, _agent_rules_map

    rules = _agent_rules_map(fake_home)
    for path in rules.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# reglas\n")

    monkeypatch.setenv("CAUSADB_AGENT", "gemini")
    assert detect_agent() == "gemini", \
        "CAUSADB_AGENT debe ganar sobre la heurística de archivos existentes"


def test_detect_agent_explicit_beats_env(fake_home, monkeypatch):
    """--agent prevalece sobre CAUSADB_AGENT (flag > env > heurística)."""
    from causadb.cli._cmd_init import detect_agent

    monkeypatch.setenv("CAUSADB_AGENT", "gemini")
    assert detect_agent("claude") == "claude", \
        "el flag explícito debe prevalecer sobre la env var"


def test_detect_agent_invalid_explicit_falls_through(fake_home):
    """Un --agent desconocido NO crashea — cae al siguiente nivel.

    Anti-teatro: si validáramos con ``ValueError`` el operador pierde el
    flujo por un typo. El override se ignora silenciosamente y se cae a
    la heurística.
    """
    from causadb.cli._cmd_init import detect_agent, _agent_rules_map

    rules = _agent_rules_map(fake_home)
    for path in rules.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# reglas\n")

    assert detect_agent("unknown-agent") == "opencode", \
        "override inválido debe caer a la heurística, no crashear"


def test_cmd_init_agent_flag_seeds_correct_rules_file(tmp_path, fake_home, monkeypatch):
    """End-to-end: ``causadb init --agent gemini`` conecta a workspace
    existente Y siembra el puntero en ``GEMINI.md`` — NO en ``AGENTS.md``.

    Esto es el test de regresión del bug (deuda #23): sin el fix,
    ``cmd_init`` llamaba ``detect_agent()`` (sin arg) → siempre
    ``"opencode"`` en multi-agente → sembraba AGENTS.md aunque el operador
    tirara el init desde gemini-cli. Con el fix el flag supera la heurística.

    Rigor multi-agente: AMBOS archivos de reglas (AGENTS.md + GEMINI.md)
    pre-existen y se asertan ilesa el AGENTS.md (no solo "inexistente")
    para cerrar el hueco del caso real donde el operador ya tiene reglas
    opencode anteriores y no quiere romperlas.
    """
    from causadb.cli._cmd_init import cmd_init, _agent_rules_map

    # Pre-crear ambos archivos de reglas — el escenario multi-agente real.
    agents_md = fake_home / ".config" / "opencode" / "AGENTS.md"
    agents_md.parent.mkdir(parents=True, exist_ok=True)
    agents_md.write_text("# reglas previas opencode\n")

    gemini_md = fake_home / ".gemini" / "GEMINI.md"
    gemini_md.parent.mkdir(parents=True, exist_ok=True)
    gemini_md.write_text("# reglas previas gemini\n")

    ancestor = tmp_path / "proyecto"
    ancestor.mkdir()
    sub_dir = ancestor / "sub"
    sub_dir.mkdir()
    WorkspaceManager.init(str(ancestor))

    monkeypatch.setattr("sys.stdin", io.StringIO("Y\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    args = _g5b_args(workspace=str(sub_dir), agent="gemini")
    code, out = cmd_init(args)
    assert code == 0, f"cmd_init falló: {out}"
    payload = json.loads(out)
    assert payload["connected"] is True
    # A3 — el assert crítico: cmd_init respeta --agent, no cae a heurística.
    assert payload["seed_doctrina_to_agent"] == "gemini", \
        "cmd_init debe respetar --agent, no caer a la heurística opencode"

    seed_file = payload["seed_doctrina_to_file"]
    assert seed_file is not None
    assert seed_file.endswith(".gemini/GEMINI.md"), \
        f"se sembró en el archivo equivocado: {seed_file} (esperaba GEMINI.md)"

    # GEMINI.md fue actualizado con el puntero CausaDB.
    new_gemini = gemini_md.read_text()
    assert "CAUSADB-GAP5" in new_gemini
    assert new_gemini.count("CAUSADB-GAP5") == 2

    # AGENTS.md queda ILESO — el hueco del bug real: antes recibía el puntero
    # "por default" y pisaba reglas opencode. Anti-teatro: no basta con que
    # "no se haya creado" (ya existía); aseveramos que no se-touch.
    agents_content = agents_md.read_text()
    assert "CAUSADB-GAP5" not in agents_content, \
        "AGENTS.md no debe recibir el puntero cuando --agent=gemini"
    assert agents_content == "# reglas previas opencode\n", \
        "AGENTS.md no debe ser modificado cuando --agent=gemini"


def test_cmd_init_no_agent_falls_back_to_heuristic_on_multi_agent(tmp_path, fake_home, monkeypatch):
    """Sin --agent y con CAUSADB_AGENT unset, cmd_init cae a la heurística
    (opencode en este entorno multi-agente). Comportamiento pre-fix,
    preservado para no romper al operador que no pasa el flag."""
    from causadb.cli._cmd_init import cmd_init, _agent_rules_map

    # Pre-crear ambos archivos de reglas — multi-agente.
    for path in _agent_rules_map(fake_home).values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# reglas\n")
    monkeypatch.delenv("CAUSADB_AGENT", raising=False)

    ancestor = tmp_path / "proyecto"
    ancestor.mkdir()
    sub_dir = ancestor / "sub"
    sub_dir.mkdir()
    WorkspaceManager.init(str(ancestor))

    monkeypatch.setattr("sys.stdin", io.StringIO("Y\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    args = _g5b_args(workspace=str(sub_dir))  # sin agent
    code, out = cmd_init(args)
    assert code == 0
    payload = json.loads(out)
    assert payload["seed_doctrina_to_agent"] == "opencode", \
        "sin --agent ni env, la heurística elige opencode (multi-agente)"
