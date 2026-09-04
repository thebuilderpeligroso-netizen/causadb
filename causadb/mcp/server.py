"""CausaDB MCP server (P.15).

Exposes 19 tools + 3 resources (causadb://events, causadb://state, causadb://config).
The server is a thin adapter — it delegates to `mcp/_tools.py`, which in turn
delegates to the existing nucleus (Article II).

Article V (Memory Layer Separation): the server does NOT read the ledger at
construction time. Reads are deferred to tool/resource call time.
`create_server()` only constructs the MCPBase instance and registers tools
and resources; no I/O is performed on the ledger.

BIT-14.7 (Auto-init): the server resolves the ledger path at startup using
this precedence:
  1. Explicit `ledger_path` passed to the tool (highest)
  2. ``CAUSADB_LEDGER_PATH`` environment variable
  3. Discover existing ``.causadb/`` workspace from CWD
  4. Auto-init: create a new workspace in CWD (lowest)

Pass ``--no-auto-init`` to ``causadb-mcp`` to disable auto-init (requires
explicit ``ledger_path`` on every tool call — legacy behavior).

F.1 (MCP Resources):
  - causadb://events — returns a JSON array of all ledger events
  - causadb://state  — returns the reconstructed state as JSON
  - causadb://config — returns the resolved server configuration (ledger path, workspace, version)

F.3 (Tool Rename): all tools use short names (log, replay, sentinel, …)
  without the causadb_ prefix. The internal `_tools.causadb_*` functions
  keep their prefixed names as private module-internal dispatch targets.
"""
import ipaddress
import json
import os
import sys
from pathlib import Path
from typing import Optional

import anyio

try:
    from mcp.server.mcpserver import MCPServer as MCPBase  # mcp v2
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPBase      # mcp v1

from causadb._config import CausaDBConfig
from causadb._ledger_index import LedgerIndex
from causadb._redactor import redact_payload
from causadb._replay_engine import ReplayEngine
from causadb._workspace import WorkspaceManager
from causadb.mcp import _tools


def _resolve_ledger() -> str:
    """Resolve ledger path: env > discover > last workspace > auto-init.

    Precedence:
      1. ``CAUSADB_LEDGER_PATH`` env var (explicit override).
      2. ``WorkspaceManager.discover(os.getcwd())`` — existing workspace.
      3. ``get_last_workspace()`` — last recorded workspace (new chat after a
         "cut of light" lands in a random directory, not the project).
      4. Auto-init in CWD via ``WorkspaceManager.init(cwd)``.

    Returns:
        Absolute path to the ledger file.

    Raises:
        RuntimeError: if the CWD is not writable and auto-init is needed.
    """
    from causadb._workspace import get_last_workspace, record_last_workspace

    def _use(path: str) -> str:
        record_last_workspace(path)
        return path

    # 1. Env var override
    env_path = os.environ.get("CAUSADB_LEDGER_PATH")
    if env_path:
        return _use(os.path.abspath(env_path))

    # 2. Discover existing workspace
    config_path = WorkspaceManager.discover(os.getcwd())
    if config_path:
        ws = WorkspaceManager.load(config_path)
        return _use(ws.ledger_path)

    # 3. Last recorded workspace (before auto-init to avoid junk ledgers)
    last = get_last_workspace()
    if last:
        return _use(last)

    # 4. Auto-init in CWD (solo si no existe .causadb/ con config inválido)
    cwd = os.getcwd()
    existing_causadb = os.path.join(cwd, ".causadb")
    if os.path.isdir(existing_causadb):
        # .causadb existe pero su config no es un workspace válido
        # (p.ej. el config global de telemetría ~/.causadb/). Auto-init
        # crashearía con FileExistsError — reportar claro (G5.B).
        raise RuntimeError(
            f"Cannot auto-init in {cwd}: .causadb/ exists but is not a "
            "valid workspace (config.json has no ledger_path). Run "
            "`causadb init <path>` or set the CAUSADB_LEDGER_PATH "
            "environment variable."
        )
    if not os.access(cwd, os.W_OK):
        raise RuntimeError(
            f"Cannot auto-init in {cwd}: directory is not writable. "
            "Run `causadb init <path>` in a writable directory or "
            "set the CAUSADB_LEDGER_PATH environment variable."
        )
    result = WorkspaceManager.init(cwd)
    print(f"CausaDB: creado workspace en {result['ledger_path']}", file=sys.stderr)
    return result["ledger_path"]


def create_server(config: Optional[CausaDBConfig] = None,
                  config_ledger_path: Optional[str] = None) -> MCPBase:
    """Construct a fresh MCPBase server with 19 tools + 3 resources.

    Args:
        config: optional `CausaDBConfig`. If provided, its `ledger_path` is
            used as the default for tools that omit ``ledger_path``.
        config_ledger_path: convenience parameter — if `config` is None and
            this is provided, a `CausaDBConfig(ledger_path=...)` is built.
            Used by tests to verify Article V (no ledger read at construction).

    Returns:
        A `MCPBase` instance with exactly 19 tools and 3 resources registered.

    Article V: this function performs NO I/O on the ledger. It only constructs
    the MCPBase instance and registers tools/resources. The ledger is read
    only when a tool or resource is invoked.
    """
    # Resolve config WITHOUT touching the ledger. CausaDBConfig.__post_init__
    # only validates that ledger_path is absolute and derives chronicle/ocb
    # paths — it does NOT read the ledger file.
    if config is None and config_ledger_path is not None:
        config = CausaDBConfig(ledger_path=config_ledger_path)
    default_ledger = config.ledger_path if config else None

    def _ledger(p: Optional[str]) -> str:
        """Resolve effective ledger: explicit arg > default > error."""
        if p is not None:
            return p
        if default_ledger is not None:
            return default_ledger
        raise ValueError(
            "No ledger path provided. Pass `ledger_path` explicitly, "
            "set the CAUSADB_LEDGER_PATH environment variable, or remove "
            "the --no-auto-init flag to enable auto-init."
        )

    mcp = MCPBase(
        name="causadb",
        instructions="CausaDB causal ledger MCP server",
    )

    # ------------------------------------------------------------------
    # F.1 — MCP Resources
    # ------------------------------------------------------------------

    @mcp.resource(uri="causadb://events")
    def events_resource() -> str:
        """Return ledger events as a JSON array (capped to DEFAULT_QUERY_LIMIT).

        Uses the configured default ledger (no client-side filtering).
        For filtered queries use the `query` tool instead.

        BIT-CHR.35 P3: el resource respeta el cap anti-gigantismo
        (``DEFAULT_QUERY_LIMIT``) — no devuelve la lista completa de
        eventos cuando el ledger excede el cap. Para exploración
        completa, usar la tool ``query`` con ``limit`` explícito y
        paginación (vía ``from_time``/``to_time``).

        C1 (cap de bytes): si el output serializado excede
        ``MAX_RESPONSE_BYTES`` (env ``CAUSADB_MAX_RESPONSE_BYTES``),
        devuelve un JSON truncado ``{"truncated": true, "events": [...],
        "message": ...}`` en vez del array (read-only; los ledgers chicos
        siguen devolviendo el array).
        """
        index = LedgerIndex(_ledger(None))
        # limit=None aplica DEFAULT_QUERY_LIMIT (cap) automáticamente.
        results = index.query()
        max_bytes = _tools._max_response_bytes()
        kept, cap_info = _tools._apply_byte_cap(results, max_bytes)
        if cap_info["truncated"]:
            return json.dumps({
                "truncated": True,
                "events": kept,
                "message": _tools._truncation_message(cap_info),
            }, default=str, sort_keys=True)
        return json.dumps(kept, default=str, sort_keys=True)

    @mcp.resource(uri="causadb://state")
    def state_resource() -> str:
        """Return the reconstructed ledger state as JSON.

        Uses the configured default ledger.
        """
        state = ReplayEngine(_ledger(None)).reconstruct_state()
        return json.dumps(state, default=str, sort_keys=True)

    @mcp.resource(uri="causadb://config")
    def config_resource() -> str:
        """Return the current server configuration (ledger path, workspace, version).

        Clients use this resource to auto-discover which ledger the server
        is connected to without manual configuration.
        """
        from causadb._updater import get_current_version
        return json.dumps({
            "ledger_path": default_ledger,
            "workspace_path": os.path.dirname(os.path.dirname(default_ledger)) if default_ledger else None,
            "version": get_current_version(),
        }, sort_keys=True)

    @mcp.resource(uri="causadb://canon")
    def canon_resource() -> str:
        """Return the agent guide (docs/canon.md).

        Doctrina (BIT-49 / briefing:92): el canon viaja DENTRO del
        producto. Cualquier agente MCP lo lee sin conocer rutas de
        archivo — resource agnóstico a tool. Los archivos de reglas de
        cada tool llevan solo un puntero a este resource (o a
        ``causadb canon``).
        """
        from causadb.cli._cmd_canon import _resolve_canon_path
        canon_path = _resolve_canon_path()
        if canon_path is None:
            return json.dumps({
                "error": "Canon (docs/canon.md) no encontrado en el paquete causadb.",
            })
        return Path(canon_path).read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # F.3 — Tools (short names, no @causadb_ prefix)
    # ------------------------------------------------------------------

    @mcp.tool()
    def log(event_json: str, ledger_path: Optional[str] = None) -> str:
        """Append an event to the CausaDB ledger.

        Args:
            event_json: JSON string of the event to append.
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).

        Returns:
            JSON string with `{"event_id", "hash", "timestamp"}`.

        Raises:
            ValueError: on JSON parse error, schema validation failure, or
            append failure (Fall-Closed — MCPBase converts to error response).
        """
        return _tools.causadb_log(event_json=event_json, ledger_path=_ledger(ledger_path))

    @mcp.tool()
    def replay(ledger_path: Optional[str] = None) -> str:
        """Reconstruct state from the CausaDB ledger.

        Args:
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).

        Returns:
            JSON string of the reconstructed state dict.
        """
        return _tools.causadb_replay(ledger_path=_ledger(ledger_path))

    @mcp.tool()
    def sentinel(ledger_path: Optional[str] = None) -> str:
        """Run sentinel rules against the CausaDB ledger.

        Args:
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).

        Returns:
            JSON string with `{"all_rules_pass", "summary", "results"}`.
        """
        return _tools.causadb_sentinel(ledger_path=_ledger(ledger_path))

    @mcp.tool()
    def query(
        ledger_path: Optional[str] = None,
        event_type: Optional[str] = None,
        ctx_id: Optional[str] = None,
        parent_event_id: Optional[str] = None,
        source: Optional[str] = None,
        text: Optional[str] = None,
        from_time: Optional[str] = None,
        to_time: Optional[str] = None,
        limit: Optional[int] = None,
        include_payloads: bool = True,
        intent_only: bool = True,
        include_excerpts: bool = False,
    ) -> str:
        """Query ledger events by filters. All optional, AND-combined.

        Args:
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).
            event_type: filter by event type (e.g. FILE_MODIFIED).
            ctx_id: filter by context ID.
            parent_event_id: filter by parent event ID.
            source: filter by source string.
            text: case-insensitive substring search in event payload.
            from_time: ISO 8601 string (inclusive lower bound).
            to_time: ISO 8601 string (inclusive upper bound).
            limit: máximo número de entradas a devolver. ``None`` aplica
                el cap por defecto (1000) anti-gigantismo. Valores
                mayores que el cap duro se clampean (no error).
            include_payloads: si ``True`` (default) los payloads se
                resuelven completos. Si ``False``, se reducen a claves
                de trazabilidad (corta ~90% bytes, no resuelve blobs).
            intent_only: si ``True`` (default) las búsquedas por ``text``
                excluyen REASONING_STEP y TOOL_CALLED (ruido de razonamiento,
                ~98% del peso de blobs) ANTES de resolver payloads. Un
                ``event_type`` explícito gana sobre esta exclusión.
            include_excerpts: si ``True`` y se busca por ``text``, cada
                resultado lleva un campo ``excerpt`` (ventana ±120 chars
                alrededor del match) para decidir relevancia sin leer el
                payload completo.

        Returns:
            JSON string con envelope SIEMPRE presente ``{"events",
            "truncated", "bytes", "dropped_events", "message"}`` (nunca
            array pelado). ``truncated: true`` cuando el output excede
            ``MAX_RESPONSE_BYTES`` (env ``CAUSADB_MAX_RESPONSE_BYTES``);
            ``message`` explica qué se cortó y cómo pedir más. Para
            exploración de ledgers grandes se recomienda
            ``include_payloads=false`` + ``include_excerpts=true``.
        """
        return _tools.causadb_query(
            ledger_path=_ledger(ledger_path),
            event_type=event_type,
            ctx_id=ctx_id,
            parent_event_id=parent_event_id,
            source=source,
            text=text,
            from_time=from_time,
            to_time=to_time,
            limit=limit,
            include_payloads=include_payloads,
            intent_only=intent_only,
            include_excerpts=include_excerpts,
        )

    @mcp.tool()
    def validate(ledger_path: Optional[str] = None) -> str:
        """Validate the ledger hash chain integrity.

        Args:
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).

        Returns:
            JSON with `{"is_valid", "failure_type", "position", "description"}`.
        """
        return _tools.causadb_validate(ledger_path=_ledger(ledger_path))

    @mcp.tool()
    def feedback(ledger_path: Optional[str] = None) -> str:
        """List HUMAN_FEEDBACK events from the ledger.

        Args:
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).

        Returns:
            JSON string list of matching entries.
        """
        return _tools.causadb_feedback(ledger_path=_ledger(ledger_path))

    @mcp.tool()
    def sandbox(ledger_path: Optional[str] = None) -> str:
        """Reconstruct state and return sandbox violations summary.

        Args:
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).

        Returns:
            JSON with `{"violations", "total_mutations"}`.
        """
        return _tools.causadb_sandbox(ledger_path=_ledger(ledger_path))

    @mcp.tool()
    def stream(ledger_path: Optional[str] = None) -> str:
        """List STREAM_INTERRUPTED events from the ledger.

        Args:
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).

        Returns:
            JSON string list of matching entries.
        """
        return _tools.causadb_stream(ledger_path=_ledger(ledger_path))

    # F.12.4 impact — downstream causal cone.
    @mcp.tool()
    def impact(event_id: str, ledger_path: Optional[str] = None) -> str:
        """Return the downstream causal cone of an event (blast radius).

        Args:
            event_id: the source event ID to trace downstream from.
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).

        Returns:
            JSON string with the list of transitively tainted events.
        """
        return _tools.causadb_impact(event_id=event_id, ledger_path=_ledger(ledger_path))

    # F.12.2 why — attribute a line to the event that introduced it.
    @mcp.tool()
    def why(file_path: str, line_number: int, ledger_path: Optional[str] = None) -> str:
        """Attribute a line to the event that introduced it (causal blame).

        Args:
            file_path: relative path of the file within the workspace snapshot.
            line_number: 1-based line number to attribute.
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).

        Returns:
            JSON string with ``{"introducer": {...}}`` or ``{"introducer": null}``.

        Raises:
            ValueError: if the file was never touched in the ledger.
        """
        return _tools.causadb_why(
            file_path=file_path,
            line_number=line_number,
            ledger_path=_ledger(ledger_path),
        )

    # F.12.3 trace — upstream causal cone of a line.
    @mcp.tool()
    def trace(file_path: str, line_number: int, ledger_path: Optional[str] = None) -> str:
        """Return the upstream causal cone of a line (transitive causes).

        Args:
            file_path: relative path of the file within the workspace snapshot.
            line_number: 1-based line number to trace upstream from.
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).

        Returns:
            JSON string with the causal cone tree (writer_event, cone, visited, depth).
        """
        return _tools.causadb_trace(
            file_path=file_path,
            line_number=line_number,
            ledger_path=_ledger(ledger_path),
        )

    # F.13.3 score — efficiency score (churn + waste + survival).
    @mcp.tool()
    def score(ledger_path: Optional[str] = None, session: Optional[str] = None) -> str:
        """Compute the efficiency score for a CausaDB ledger (or specific session).

        The score (0-100) combines churn (lines written then deleted),
        waste (LLM cost on reverted code), and survival (code in final state).

        Args:
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).
            session: optional ctx_id to score just one session.

        Returns:
            JSON with {overall_score, churn_score, waste_score, survival_score,
            weights_used, correlation_method}.
            NOTE: correlation_method="timestamp_proximity" indicates imprecise
            LLM-waste attribution.
        """
        return _tools.causadb_score(ledger_path=_ledger(ledger_path), session=session)

    # F.13.4 skill_list — list available CausaDB skills (learned context patterns).
    @mcp.tool()
    def skill_list(
        ledger_path: Optional[str] = None,
        skill_types: Optional[str] = None,
        limit: Optional[int] = None,
        order: str = "desc",
    ) -> str:
        """List available CausaDB skills (learned context patterns).

        Skills are ledger-persisted patterns from prior sessions (file trees,
        conventions, tool patterns, decisions) used to compress context.

        Args:
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).
            skill_types: optional comma-separated filter (e.g. "file_tree,decisions").
            limit: optional int — return only the first ``limit`` skills
                (after ordering). Si None, retorna todos.
            order: orden por timestamp — "desc" (default, mas recientes
                primero) o "asc".

        Returns:
            JSON with {count, skills: [...], limit, order}. Empty count means
            no skills yet registered for this ledger.
        """
        return _tools.causadb_skill_list(
            ledger_path=_ledger(ledger_path),
            skill_types=skill_types,
            limit=limit,
            order=order,
        )

    # GOVERNANCE_DECISION — log a governance decision (Capa 1 — Agent).
    @mcp.tool()
    def log_decision(
        reasoning: str,
        impact: str,
        decision_type: str,
        origin: str,
        ledger_path: Optional[str] = None,
        alternatives_considered: Optional[list[str]] = None,
        intent_hash: Optional[str] = None,
        confidence: Optional[float] = None,
        ctx_id: Optional[str] = None,
        bit: Optional[str] = None,
    ) -> str:
        """Log a governance decision to the CausaDB ledger.

        Records a structured GOVERNANCE_DECISION event capturing agent
        decisions about strategic, architectural, tactical, or revert
        choices that affect project direction.

        Args:
            reasoning: The decision reasoning text.
            impact: Impact level — "critical", "high", "medium", or "low".
            decision_type: Type — "strategic", "architectural", "tactical", or "revert".
            origin: Origin — "agent" (explicit) or "distill" (heuristic).
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).
            alternatives_considered: optional list of alternative approaches.
            intent_hash: optional hash linking to a REASONING_STEP.
            confidence: optional float in [0.0, 1.0].
            ctx_id: optional context ID.
            bit: optional BIT name to link the decision to (GAP-02).

        Returns:
            JSON string with {"event_id", "hash", "timestamp"}.
        """
        return _tools.causadb_log_decision(
            reasoning=reasoning,
            impact=impact,
            decision_type=decision_type,
            origin=origin,
            ledger_path=_ledger(ledger_path),
            alternatives_considered=alternatives_considered,
            intent_hash=intent_hash,
            confidence=confidence,
            ctx_id=ctx_id,
            bit=bit,
        )

    # chronicle_append — sedimentar BIT-entry al CAUSADB_CHRONICLE.md.
    @mcp.tool()
    def chronicle_append(
        ledger_path: Optional[str] = None,
        bit: str = "",
        title: str = "",
        date: str = "",
        author: str = "",
        nature: str = "",
        summary: Optional[str] = None,
        files: Optional[list[str]] = None,
        body: str = "",
        event_id: Optional[str] = None,
    ) -> str:
        """Sedimentar un BIT-entry al CAUSADB_CHRONICLE.md con template curado.

        Reemplaza el edit manual del agente sobre el Chronicle (Layer 3,
        humana). Idempotente: bit_id duplicado → {"status": "already_exists"}
        sin duplicar. FAIL-CLOSED: chronicle no resuelto o campos faltantes
        → error. La alineación ledger ↔ .md la garantiza el bit_id
        compartido + el event_id opcional en **Referencias:**.

        Args:
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).
            bit: BIT id (e.g. "BIT-CHR.106").
            title: entry title.
            date: entry date YYYY-MM-DD.
            author: entry author (Maker/Checker).
            nature: entry nature (e.g. "FIX CERRADO").
            summary: optional summary (el template curado lo cubre en el body).
            files: optional list of files touched (cubierto por el body).
            body: markdown body of the entry (required).
            event_id: optional event_id to cite in **Referencias:**.

        Returns:
            JSON string with {"status", "bit_id", "chronicle_path"}.

        Raises:
            ValueError: on FAIL-CLOSED (chronicle no resuelto / campos faltantes).
        """
        return _tools.causadb_chronicle_append(
            ledger_path=_ledger(ledger_path),
            bit=bit,
            title=title,
            date=date,
            author=author,
            nature=nature,
            summary=summary,
            files=files,
            body=body,
            event_id=event_id,
        )

    # BIT-14.6 revive — generate volatile revival context.
    @mcp.tool()
    def revive(
        ledger_path: Optional[str] = None,
        output_format: str = "markdown",
        max_decisions: int = 10,
    ) -> str:
        """Generate volatile revival context from the CausaDB ledger.

        Combines technical state (resume) with governance decisions and tool
        instructions into a single revival context for agent bootstrap.

        Args:
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).
            output_format: "markdown" (default) or "json".
            max_decisions: maximum number of governance decisions to include (default 10).

        Returns:
            Markdown or JSON string with the revival context.
        """
        return _tools.causadb_revive(
            ledger_path=_ledger(ledger_path),
            output_format=output_format,
            max_decisions=max_decisions,
        )

    # F1 (M2) — ocb_status — memoria granular de corto plazo (L1 Art. V).
    @mcp.tool()
    def ocb_status(ledger_path: Optional[str] = None, include_metadata: bool = True) -> str:
        """Return OCB session context + partition overview.

        Merges the OCB session context (session_type, summary, the 2 most
        recent preloaded partitions) with the full partition list and
        per-partition metadata (id, first/last timestamp, session_ids,
        sources, event_types, event_count).

        Args:
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).
            include_metadata: if True (default) includes ``partition_metadata``
                (capped to the 50 most recent partitions, anti-gigantismo
                BIT-CHR.35 P3). If False, only partition IDs are returned.

        Returns:
            JSON string with `{session_type, summary, preloaded_partitions,
            all_partition_ids, total_partitions, [partition_metadata]}`.
        """
        return _tools.causadb_ocb_status(
            ledger_path=_ledger(ledger_path),
            include_metadata=include_metadata,
        )

    # F1 (M2) — ocb_load_partition — detalle de una partición específica.
    @mcp.tool()
    def ocb_load_partition(partition_id: str, ledger_path: Optional[str] = None,
                           resolve_blobs: bool = True) -> str:
        """Load a specific OCB partition, resolving $blob refs on demand.

        Args:
            partition_id: name of the partition (e.g. OCB_PARTITION_<ns>.log).
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).
            resolve_blobs: if True (default) $blob payloads are resolved
                against the BlobStore; if False they are returned as
                `{"resolved": False, "$blob": hash}` (metadata only).

        Returns:
            JSON string list of event dicts (truncated to 1000 with
            `{"truncated": True, "count": N}` when the cap applies).
        """
        return _tools.causadb_ocb_load_partition(
            ledger_path=_ledger(ledger_path),
            partition_id=partition_id,
            resolve_blobs=resolve_blobs,
        )

    # F.13 — recover — storyboard completo de una sesión desde la fuente cruda.
    @mcp.tool()
    def recover(ledger_path: Optional[str] = None, session_id: str = "",
                tool: Optional[str] = None, search: Optional[str] = None) -> str:
        """Recupera el storyboard completo de una sesión de agente desde su
        fuente cruda (opencode.db, gemini jsonl, etc.).

        Args:
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).
            session_id: id de la sesión a recuperar (ver docstring por tool).
            tool: herramienta explícita (opencode|gemini|claude|grok|hermes|
                openjarvis|codex|cursor|windsurf). Si se omite, se auto-detecta.
            search: keyword a buscar en los storyboards persistidos (Fase 12).
                Gana sobre `session_id` si ambos vienen (paridad CLI).

        Returns:
            JSON string con el envelope `{"tool", "storyboard"}` (por
            session_id) o `{"matches": [...]}` (por search).

        Raises:
            ValueError: on ANY error (Fall-Closed, Article VIII).
        """
        return _tools.causadb_recover(
            ledger_path=_ledger(ledger_path),
            session_id=session_id,
            tool=tool,
            search=search,
        )

    # F.14 — Shared Documents (Coordinación Multi-Agente)
    @mcp.tool()
    def shared_document_read(name: str, ledger_path: Optional[str] = None) -> str:
        """Lee anotador fijo de coordinación multi-agente.

        Args:
            name: "AUDIT_REPORT" o "ACTION_PLAN".
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).

        Returns:
            JSON string with the document content.

        Raises:
            ValueError: if name is not allowed.
        """
        return _tools.causadb_shared_document_read(
            name=name,
            ledger_path=_ledger(ledger_path),
        )

    @mcp.tool()
    def shared_document_write(name: str, content: str, ledger_path: Optional[str] = None) -> str:
        """Escribe anotador fijo de coordinación multi-agente.

        Args:
            name: "AUDIT_REPORT" o "ACTION_PLAN".
            content: JSON string with the document content.
            ledger_path: absolute path to the ledger file (optional if
                auto-init is enabled or CAUSADB_LEDGER_PATH is set).

        Returns:
            JSON string with {"status": "ok", "name": name}.

        Raises:
            ValueError: if name is not allowed or content is invalid JSON.
        """
        return _tools.causadb_shared_document_write(
            name=name,
            content=content,
            ledger_path=_ledger(ledger_path),
        )

    return mcp


# Module-level instance for the `causadb-mcp` console script entry point.
# Tests should use `create_server()` to get a fresh instance per test.
mcp = None


# ---------------------------------------------------------------------------
# Network mode (streamable-http) security — proof of interoperability.
#
# These helpers are applied ONLY in `main()` when `--transport != stdio`.
# `create_server()` is intentionally NOT touched: the ~21 tests that use it
# keep getting the full 21-tool server. The security subset is a separate
# layer applied on top of a freshly built server.
# ---------------------------------------------------------------------------

# Tools that remain exposed in network mode (read-only, safe).
HTTP_SAFE_TOOLS = {"revive", "query", "ocb_status", "validate", "sentinel"}

# Resources that remain exposed in network mode. causadb://events and
# causadb://state dump the whole ledger and are excluded (see
# `_apply_http_resource_subset`).
HTTP_SAFE_RESOURCES = {"causadb://config", "causadb://canon"}


def _is_loopback(host: str) -> bool:
    """Return True if `host` binds to loopback only.

    Treats "localhost" and "" as loopback. Uses `ipaddress` to detect
    loopback IPs (127.0.0.1, ::1, …). Any other hostname/IP is NOT loopback
    (fail-closed: only known-loopback is considered safe).
    """
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Not a bare IP (e.g. a hostname). Fail-closed: not loopback.
        return False


def _check_bind_safety(host: str, api_key: Optional[str]) -> None:
    """Fail-closed bind-safety (OpenJarvis check_bind_safety pattern).

    A non-loopback host WITHOUT an API key is refused with ``SystemExit(1)``.
    Loopback hosts are always allowed (the local proof). Non-loopback hosts
    require an explicit API key (operator opted in to network exposure).
    """
    if not _is_loopback(host) and not api_key:
        print(
            f"CausaDB: refusing to bind MCP server to non-loopback host "
            f"{host!r} without an API key. Set CAUSADB_MCP_API_KEY or use a "
            "loopback host (127.0.0.1).",
            file=sys.stderr,
        )
        sys.exit(1)


def _require_explicit_ledger(ledger: Optional[str]) -> str:
    """Network mode requires an explicit ledger (--ledger or env).

    Unlike stdio mode, network mode must NOT resolve the ledger from CWD
    (auto-init / discover) — the ledger is a security boundary. Missing
    ledger → ``SystemExit(1)``.
    """
    path = ledger or os.environ.get("CAUSADB_LEDGER_PATH")
    if path:
        return os.path.abspath(path)
    print(
        "CausaDB: network mode requires an explicit ledger. Pass --ledger "
        "<path> or set CAUSADB_LEDGER_PATH.",
        file=sys.stderr,
    )
    sys.exit(1)


async def _apply_http_tool_subset(server) -> set:
    """Remove every write/sensitive tool, leaving only the safe read-only set.

    Returns the set of tool names that remain exposed (== HTTP_SAFE_TOOLS
    when the server was built by `create_server()`). Any tool registered by
    `create_server()` that is not in HTTP_SAFE_TOOLS is removed.
    """
    registered = {t.name for t in await server.list_tools()}
    to_remove = registered - HTTP_SAFE_TOOLS
    for name in to_remove:
        server.remove_tool(name)
    return HTTP_SAFE_TOOLS & registered


def _apply_http_resource_subset(server) -> set:
    """Remove causadb://events and causadb://state resources in network mode.

    FastMCP has no ``remove_resource``, so we drop them from the internal
    resource manager (documented limitation). Returns the URIs that remain
    exposed (config + canon).
    """
    resources = server._resource_manager._resources
    for uri in list(resources.keys()):
        if uri not in HTTP_SAFE_RESOURCES:
            del resources[uri]
    return set(resources.keys())


def _redact_recursive(value, config):
    """Recursively apply `redact_payload` to every dict in a structure."""
    if isinstance(value, dict):
        redacted = redact_payload(value, config)
        return {k: _redact_recursive(v, config) for k, v in redacted.items()}
    if isinstance(value, list):
        return [_redact_recursive(v, config) for v in value]
    return value


def _redact_json_output(text: str, config) -> str:
    """Redact sensitive fields in a JSON string output (query/revive).

    Parses the JSON, redacts every dict recursively with `redact_payload`,
    and re-serializes. Non-JSON output is returned unchanged.
    """
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return text
    return json.dumps(_redact_recursive(data, config), default=str, sort_keys=True)


def _wrap_tool_with_redaction(server, tool_name: str, config) -> None:
    """Replace a tool's fn with a wrapper that redacts sensitive fields.

    We mutate the existing Tool's ``fn`` (keeping its ``fn_metadata`` /
    ``arg_model`` intact) so FastMCP still validates the original parameters.
    The wrapper calls the original fn and redacts its JSON output.
    """
    tool = server._tool_manager.get_tool(tool_name)
    original_fn = tool.fn

    def wrapper(**kwargs):
        return _redact_json_output(original_fn(**kwargs), config)

    tool.fn = wrapper


async def _apply_http_security(server, config) -> dict:
    """Apply the full network-mode security subset to a server.

    Returns a dict describing what was applied (tools + resources remaining).
    """
    tools = await _apply_http_tool_subset(server)
    resources = _apply_http_resource_subset(server)
    for name in ("query", "revive"):
        _wrap_tool_with_redaction(server, name, config)
    return {"tools": tools, "resources": resources}


def _parse_args():
    """Minimal argv parsing for the MCP server entry point.

    Returns (transport, host, port, ledger). Uses the same sys.argv pattern
    the existing `main()` already uses (no argparse dependency).
    """
    transport = "stdio"
    host = "127.0.0.1"
    port = 8000
    ledger = None
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--transport" and i + 1 < len(argv):
            transport = argv[i + 1]
            i += 2
            continue
        if arg == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
            continue
        if arg == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                port = 8000
            i += 2
            continue
        if arg == "--ledger" and i + 1 < len(argv):
            ledger = argv[i + 1]
            i += 2
            continue
        i += 1
    return transport, host, port, ledger


def main() -> None:
    """Entry point for ``causadb-mcp`` console script.

    Resolves the ledger path at startup (env > discover > auto-init)
    unless ``--no-auto-init`` is passed, in which case the legacy
    behavior applies (each tool requires an explicit ``ledger_path``).

    ``--transport`` selects the transport:
      - "stdio" (default): current behavior, unchanged.
      - "streamable-http": exposes the server over HTTP with a security
        subset (bind-safety, explicit ledger, read-only tools, redaction).
        The proof runs on loopback (127.0.0.1) by default.
    """
    global mcp
    transport, host, port, ledger = _parse_args()

    if transport == "stdio":
        no_auto_init = "--no-auto-init" in sys.argv
        if no_auto_init:
            mcp = create_server()
        else:
            try:
                ledger_path = _resolve_ledger()
                mcp = create_server(config_ledger_path=ledger_path)
            except (RuntimeError, FileExistsError):
                # Degradar a server sin default: tools con `ledger_path`
                # explícito siguen funcionando (G5.B). No crashear al arrancar.
                mcp = create_server()
        mcp.run(transport="stdio")
        return

    # --- Network mode (streamable-http) — security subset -----------------
    api_key = os.environ.get("CAUSADB_MCP_API_KEY")
    _check_bind_safety(host, api_key)
    ledger_path = _require_explicit_ledger(ledger)
    config = CausaDBConfig(ledger_path=ledger_path)
    mcp = create_server(config=config)
    anyio.run(_apply_http_security, mcp, config)
    # host/port live on the FastMCP constructor settings; set them post-hoc
    # (create_server() is intentionally untouched).
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
