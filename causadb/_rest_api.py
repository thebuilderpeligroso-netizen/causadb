import json
import logging
import os
import mimetypes
import shutil
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from causadb._ledger_writer import LedgerWriter
from causadb._replay_engine import ReplayEngine
from causadb._ledger_index import LedgerIndex, DEFAULT_QUERY_LIMIT, MAX_QUERY_LIMIT
from causadb._event_schema import CanonicalEvent
from causadb._watchdog import HealthMetrics
from causadb._query_engine import query_events
import csv
import io


def _causadb_executable() -> str:
    """Resuelve el ejecutable ``causadb`` del PATH (shim o pip-installed).

    Bajo systemd, ``sys.executable`` es el Python del sistema que no tiene
    el paquete ``causadb`` en sys.path. Usar el shim del PATH asegura que
    los subprocesos lancen el CLI real sin depender del entorno Python.
    """
    exe = shutil.which("causadb")
    if exe is not None:
        return exe
    import sys
    return sys.executable

# Module-level mutable state for workspace switching.
# The REST API serves one ledger at a time. When the user switches workspace
# via the dashboard, this value is updated and the handler lazily re-creates
# its writer/index caches on the next request.
_active_ledger_path = None

def set_active_ledger(path: str):
    """Update the active ledger path (thread-safe via GIL).

    Records the path as the last workspace so ``causadb revive --last``
    keeps pointing at the most recently used project.
    """
    global _active_ledger_path
    _active_ledger_path = path
    try:
        from causadb._workspace import record_last_workspace
        record_last_workspace(path)
    except Exception:
        pass


class CausaDBAPIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the CausaDB REST API.

    Supports optional auth via :class:`~causadb._auth.AuthManager`.
    When ``auth_manager`` is ``None`` or disabled, all requests pass through
    without authentication (localhost default).
    """

    DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), "dashboard")

    # Path → action mapping for authorization checks.
    # Dashboard paths are excluded (public).
    # /api/webhook/tradingview is intentionally absent — it is a public
    # webhook receiver (TradingView cannot send API keys).
    _PATH_ACTIONS = {
        "/api/log": "log",
        "/api/query": "query",
        "/api/replay": "replay",
        "/api/events": "query",
        "/api/register-type": "register_type",
        "/api/export": "query",
        "/api/trace": "query",
        "/api/score": "query",
        "/api/check-update": "query",
        "/api/update": "admin",
        "/api/crashes": "query",
        "/api/config": "query",
        "/api/daemon/start": "admin",
        "/api/daemon/stop": "admin",
        "/api/daemon/status": "query",
        "/api/workspaces": "query",
        "/api/workspace/switch": "admin",
    }

    def __init__(self, ledger_path, *args, auth_manager=None, user_store=None, **kwargs):
        self._ledger_path = ledger_path
        self._writer = LedgerWriter(ledger_path)
        self._index = LedgerIndex(ledger_path)
        self._auth_manager = auth_manager
        self._user_store = user_store
        global _active_ledger_path
        if _active_ledger_path is None:
            _active_ledger_path = ledger_path
        super().__init__(*args, **kwargs)
    
    def _json_response(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())
    
    def _check_auth(self, action: str) -> bool:
        """Check authentication and authorization for *action*.

        If no ``auth_manager`` is configured, all requests are allowed.
        Otherwise, extracts the ``X-API-Key`` header, authenticates, and
        authorizes.

        Returns:
            ``True`` if the request should proceed, ``False`` if a response
            (401 or 403) has already been sent.

        Artículo IX: Fall-Closed — invalid/missing key → 401,
        insufficient permissions → 403.
        """
        if self._auth_manager is None:
            return True  # No auth configured, allow all
        api_key = self.headers.get("X-API-Key")
        role = self._auth_manager.authenticate(api_key)
        if role is None:
            self._json_response({"error": "unauthorized", "message": "Invalid or missing API key"}, 401)
            return False
        if not self._auth_manager.authorize(role, action):
            self._json_response({"error": "forbidden", "message": f"Role '{role}' cannot perform '{action}'"}, 403)
            return False
        return True

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return None
        return json.loads(self.rfile.read(length))
    
    def do_GET(self):
        parsed = urlparse(self.path)

        # Dashboard paths are public — no auth required.
        if parsed.path == "/dashboard" or parsed.path == "/dashboard/":
            self._serve_dashboard_file("index.html")
            return
        if parsed.path.startswith("/dashboard/"):
            filename = parsed.path[len("/dashboard/"):]
            self._serve_dashboard_file(filename)
            return

        # Health endpoint is public (no auth).
        if parsed.path == "/api/health":
            metrics = HealthMetrics(self._ledger_path, self._index)
            self._json_response({"status": "ok", **metrics.to_dict()})
            return

        # /api/auth/me requires auth (X-API-Key).
        if parsed.path == "/api/auth/me":
            self._handle_auth_me()
            return

        # All other API paths require auth.
        action = self._PATH_ACTIONS.get(parsed.path)
        if action is None:
            self._json_response({"error": "not found"}, 404)
            return
        if not self._check_auth(action):
            return

        if parsed.path == "/api/score":
            if not self._check_auth("query"):
                return
            self._handle_score()
            return

        if parsed.path == "/api/config":
            if not self._check_auth("query"):
                return
            self._handle_get_config()
            return

        if parsed.path == "/api/check-update":
            if not self._check_auth("query"):
                return
            self._handle_check_update()
            return

        if parsed.path == "/api/crashes":
            if not self._check_auth("query"):
                return
            self._handle_get_crashes()
            return

        if parsed.path == "/api/daemon/status":
            if not self._check_auth("query"):
                return
            self._handle_daemon_status()
            return

        if parsed.path == "/api/workspaces":
            if not self._check_auth("query"):
                return
            self._handle_list_workspaces()
            return

        if parsed.path == "/api/query":
            params = parse_qs(parsed.query)
            self._handle_get_query(params)
        elif parsed.path == "/api/events":
            params = parse_qs(parsed.query)
            self._handle_get_events(params)
        else:
            self._json_response({"error": "not found"}, 404)
    
    def do_POST(self):
        try:
            # TradingView webhook is a public endpoint — no auth required.
            if self.path == "/api/webhook/tradingview":
                body = self._read_body()
                self._handle_webhook_tradingview(body)
                return

            # /api/assistant is a local dashboard endpoint — no auth required.
            if self.path == "/api/assistant":
                self._handle_assistant()
                return

            # /api/auth/login is public — no auth required.
            if self.path == "/api/auth/login":
                body = self._read_body()
                self._handle_auth_login(body)
                return

            # /api/crashes/export is handled before the action check since
            # its path is a sub-path of /api/crashes.
            if self.path == "/api/crashes/export":
                if not self._check_auth("admin"):
                    return
                self._handle_export_crashes()
                return

            # Daemon control endpoints (admin only for start/stop)
            if self.path == "/api/daemon/start":
                if not self._check_auth("admin"):
                    return
                self._handle_daemon_start()
                return
            if self.path == "/api/daemon/stop":
                if not self._check_auth("admin"):
                    return
                self._handle_daemon_stop()
                return
            if self.path == "/api/daemon/status":
                if not self._check_auth("query"):
                    return
                self._handle_daemon_status()
                return

            # Workspace switch (admin only)
            if self.path == "/api/workspace/switch":
                body = self._read_body()
                if not self._check_auth("admin"):
                    return
                self._handle_switch_workspace(body)
                return

            # Check auth for all other POST paths.
            action = self._PATH_ACTIONS.get(self.path)
            if action is None:
                self._json_response({"error": "not found"}, 404)
                return
            if not self._check_auth(action):
                return

            body = self._read_body()
            if body is None:
                self._json_response({"error": "empty body"}, 400)
                return
            if self.path == "/api/log":
                self._handle_log(body)
            elif self.path == "/api/replay":
                self._handle_replay(body)
            elif self.path == "/api/query":
                self._handle_query(body)
            elif self.path == "/api/export":
                self._handle_export(body)
            elif self.path == "/api/register-type":
                self._handle_register_type(body)
            elif self.path == "/api/trace":
                self._handle_trace(body)
            elif self.path == "/api/update":
                self._handle_install_update()
            else:
                self._json_response({"error": "not found"}, 404)
        except json.JSONDecodeError:
            self._json_response({"error": "invalid JSON"}, 400)
        except Exception as e:
            logging.exception("REST API error")
            self._json_response({"error": str(e)}, 500)
    
    def do_PUT(self):
        """Handle PUT requests — config updates."""
        try:
            parsed = urlparse(self.path)

            if parsed.path == "/api/config":
                if not self._check_auth("admin"):
                    return
                self._handle_update_config()
            else:
                self._json_response({"error": "not found"}, 404)
        except json.JSONDecodeError:
            self._json_response({"error": "invalid JSON"}, 400)
        except Exception as e:
            logging.exception("REST API error (PUT)")
            self._json_response({"error": str(e)}, 500)

    # ── Daemon control endpoints ────────────────────────────────────

    def _handle_daemon_status(self):
        """GET /api/daemon/status — check daemon and sub-services status."""
        from causadb._daemon import is_running
        vigilante = is_running("vigilante")
        mcp_proxy = is_running("mcp_proxy")
        proxy_server = is_running("proxy_server")
        self._json_response({
            "running": vigilante or mcp_proxy or proxy_server,
            "vigilante": vigilante,
            "mcp_proxy": mcp_proxy,
            "proxy_server": proxy_server,
        })

    def _handle_daemon_start(self):
        """POST /api/daemon/start — start sub-services via subprocess."""
        from causadb._daemon import get_daemon
        import subprocess
        daemon = get_daemon()
        results = {}
        causadb_exe = _causadb_executable()

        if not daemon.is_running("vigilante"):
            subprocess.Popen(
                [causadb_exe, "vigilante", "start", "--ledger", _active_ledger_path, "--daemon"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            results["vigilante"] = "started"
        else:
            results["vigilante"] = "already_running"

        if not daemon.is_running("mcp_proxy"):
            subprocess.Popen(
                [causadb_exe, "mcp-proxy", "start", "--ledger", _active_ledger_path, "--daemon"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            results["mcp_proxy"] = "started"
        else:
            results["mcp_proxy"] = "already_running"

        if not daemon.is_running("proxy_server"):
            subprocess.Popen(
                [causadb_exe, "proxy-server", "start", "--ledger", _active_ledger_path, "--daemon"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            results["proxy_server"] = "started"
        else:
            results["proxy_server"] = "already_running"

        self._json_response({"status": "started", **results})

    def _handle_daemon_stop(self):
        """POST /api/daemon/stop — stop all sub-services."""
        from causadb._daemon import get_daemon
        daemon = get_daemon()
        results = {}
        for name in ("vigilante", "mcp_proxy", "proxy_server"):
            if daemon.is_running(name):
                results[name] = "stopped" if daemon.kill(name) else "kill_failed"
            else:
                results[name] = "not_running"
        self._json_response({"status": "stopped", **results})

    # ── Workspace endpoints ─────────────────────────────────────────

    def _handle_list_workspaces(self):
        """GET /api/workspaces — scan for CausaDB workspaces."""
        from causadb._workspace import WorkspaceManager
        workspaces = []

        # Always include the active ledger as a workspace.
        ledger_dir = os.path.dirname(os.path.dirname(self._ledger_path))  # strip .causadb/ledger.log
        name = os.path.basename(ledger_dir) or "master"
        workspaces.append({
            "name": name,
            "ledger_path": self._ledger_path,
            "project_path": ledger_dir,
            "is_active": True,
        })

        search_dirs = [
            os.path.expanduser("~/proyectos"),
            os.path.expanduser("~/projects"),
            os.path.expanduser("~"),
        ]
        seen = {self._ledger_path}
        for base_dir in search_dirs:
            if not os.path.isdir(base_dir):
                continue
            try:
                entries = os.listdir(base_dir)
            except PermissionError:
                continue
            for entry in entries:
                candidate = os.path.join(base_dir, entry)
                if not os.path.isdir(candidate):
                    continue
                config_path = os.path.join(candidate, ".causadb", "config.json")
                if not os.path.isfile(config_path):
                    continue
                try:
                    ws = WorkspaceManager.load(config_path)
                    if ws.ledger_path in seen:
                        continue
                    seen.add(ws.ledger_path)
                    workspaces.append({
                        "name": entry,
                        "ledger_path": ws.ledger_path,
                        "project_path": candidate,
                        "is_active": ws.ledger_path == _active_ledger_path,
                    })
                except Exception:
                    continue
        self._json_response({"workspaces": workspaces})

    def _handle_switch_workspace(self, body):
        """POST /api/workspace/switch — switch active ledger."""
        if body is None:
            self._json_response({"error": "empty body"}, 400)
            return
        new_ledger = body.get("ledger_path")
        if not new_ledger or not os.path.isfile(new_ledger):
            self._json_response({"error": "invalid ledger_path"}, 400)
            return
        set_active_ledger(new_ledger)
        self._writer = LedgerWriter(new_ledger)
        self._index = LedgerIndex(new_ledger)
        self._ledger_path = new_ledger
        self._json_response({"status": "switched", "ledger_path": new_ledger})

    # ── Auth endpoints (#10 RBAC persistente) ───────────────────────

    def _handle_auth_login(self, body):
        """POST /api/auth/login — authenticate via username + password.

        Public endpoint (no API key required). Returns an ``api_key`` on
        success that can be used as ``X-API-Key`` header for subsequent
        requests.

        Requires ``_user_store`` to be configured on the handler.
        """
        if self._user_store is None:
            self._json_response({"error": "user store not configured"}, 501)
            return
        username = (body or {}).get("username", "").strip()
        password = (body or {}).get("password", "").strip()
        if not username or not password:
            self._json_response({"error": "username and password are required"}, 400)
            return
        try:
            api_key = self._user_store.authenticate(username, password)
            user = self._user_store.get_user_by_api_key(api_key)
            self._json_response({"api_key": api_key, "user": user})
        except Exception as e:
            self._json_response({"error": str(e)}, 401)

    def _handle_auth_me(self):
        """GET /api/auth/me — return current user info from X-API-Key."""
        if self._auth_manager is None:
            self._json_response({"error": "auth not configured"}, 501)
            return
        api_key = self.headers.get("X-API-Key")
        if not api_key:
            self._json_response({"error": "unauthorized"}, 401)
            return

        # Check dev-mode keys first
        role = self._auth_manager._api_keys.get(api_key)
        if role is not None:
            self._json_response({
                "username": "<dev-mode>",
                "role": role,
                "api_key": api_key,
                "auth_mode": "dev",
            })
            return

        # UserStore lookup
        us = self._auth_manager.user_store
        if us is not None:
            user = us.get_user_by_api_key(api_key)
            if user is not None:
                self._json_response({
                    "username": user["username"],
                    "role": user["role"],
                    "api_key": user["api_key"],
                    "auth_mode": "persistent",
                })
                return

        self._json_response({"error": "unauthorized"}, 401)

    # ── Config endpoints ────────────────────────────────────────────

    def _handle_get_config(self):
        """GET /api/config — return current config with telemetry_enabled."""
        from causadb._telemetry import is_enabled
        self._json_response({
            "telemetry_enabled": is_enabled(),
        })

    def _handle_update_config(self):
        """PUT /api/config — update config fields (admin only).

        Supports ``telemetry_enabled`` bool field.
        """
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        data = json.loads(body)

        if "telemetry_enabled" in data:
            from causadb._telemetry import set_enabled
            set_enabled(bool(data["telemetry_enabled"]))

        self._json_response({"status": "ok"})

    def _handle_log(self, body):
        if "event_id" not in body:
            import uuid
            body["event_id"] = str(uuid.uuid4())
        event = CanonicalEvent.from_dict(body)
        self._writer.append(event)
        self._json_response({
            "event_id": event.event_id,
            "timestamp": event.timestamp,
        })
    
    def _handle_replay(self, body):
        to_time = body.get("to_time") if body else None
        engine = ReplayEngine(self._ledger_path)
        state = engine.reconstruct_state(to_time=to_time)
        self._json_response(state)

    def _handle_register_type(self, body):
        """Register a custom event type (stub — delegates to _event_registry).

        Requires ``register_type`` permission (admin only).
        This is a thin REST wrapper around :func:`register_type`.
        """
        from causadb._event_registry import register_type, EventTypeSpec

        name = body.get("name")
        if not name:
            self._json_response({"error": "missing 'name' in body"}, 400)
            return
        fields = body.get("required_fields", [])
        spec = EventTypeSpec(required_fields=set(fields))
        register_type(name, spec)
        self._json_response({"status": "ok", "registered": name})
    
    def _handle_webhook_tradingview(self, body):
        """POST /api/webhook/tradingview — public webhook receiver.

        TradingView sends plain JSON like ``{"symbol": "BTCUSD", "side": "buy"}``.
        This endpoint maps it to a ``TRADE_EXECUTED`` CanonicalEvent and
        writes it to the ledger via LedgerWriter.

        Artículo IX (Fall-Closed): if body is empty or malformed, we still
        write a TRADE_EXECUTED with empty payload as a signal of a malformed
        webhook.
        """
        from causadb._event_schema import CanonicalEvent
        from causadb._event_types import EventType

        payload = body or {}

        # Register TRADE_EXECUTED if not already registered (idempotent).
        # The adapter module does this at import time, but we ensure it here
        # in case the adapter hasn't been imported yet.
        from causadb._event_registry import register_type, EventTypeSpec, is_registered
        if not is_registered("TRADE_EXECUTED"):
            register_type(
                "TRADE_EXECUTED",
                EventTypeSpec(required_fields={"symbol", "side", "qty", "price"}),
            )

        event = CanonicalEvent(
            event_type=EventType("TRADE_EXECUTED"),
            ctx_id="tradingview",
            source="tradingview:webhook",
            source_type="agent",
            payload=payload,
        )
        self._writer.append(event)
        self._json_response({
            "event_id": event.event_id,
            "timestamp": event.timestamp,
        })

    def _handle_query(self, body):
        events = list(self._index.query(
            event_type=body.get("event_type"),
            ctx_id=body.get("ctx_id"),
            parent_event_id=body.get("parent_event_id"),
            source=body.get("source"),
        ))
        self._json_response(events)

    def _handle_export(self, body):
        fmt = (body or {}).get("format", "json")
        from_time = (body or {}).get("from")
        to_time = (body or {}).get("to")
        event_type = (body or {}).get("event_type")
        text = (body or {}).get("q")

        results = query_events(
            self._ledger_path,
            from_time=from_time,
            to_time=to_time,
            event_type=event_type,
            text=text,
        )

        if fmt == "csv":
            output = io.StringIO()
            writer = csv.writer(output)
            if results:
                writer.writerow(results[0].keys())
                for row in results:
                    writer.writerow(row.values())
            csv_content = output.getvalue()
            output.close()
            self.send_response(200)
            self.send_header("Content-Type", "text/csv")
            self.send_header("Content-Disposition", "attachment; filename=causadb_export.csv")
            self.end_headers()
            self.wfile.write(csv_content.encode())
        else:
            self._json_response(results)

    def _handle_trace(self, body):
        event_id = (body or {}).get("event_id")
        if not event_id:
            self._json_response({"error": "missing 'event_id' in body"}, 400)
            return

        # Find the target event in the index
        all_events = list(self._index.query())
        target = None
        for entry in all_events:
            if entry["event"]["event_id"] == event_id:
                target = entry["event"]
                break

        if not target:
            self._json_response({"error": "event not found"}, 404)
            return

        # Walk UP: collect ancestors via parent_event_id
        parents = []
        current_parent_id = target.get("parent_event_id")
        while current_parent_id:
            found = False
            for entry in all_events:
                if entry["event"]["event_id"] == current_parent_id:
                    parents.append(entry["event"])
                    current_parent_id = entry["event"].get("parent_event_id")
                    found = True
                    break
            if not found:
                break

        # Walk DOWN: collect children via parent_event_id matching
        children = []
        for entry in all_events:
            if entry["event"].get("parent_event_id") == event_id:
                children.append(entry["event"])

        # Recursively get grandchildren (2 levels)
        grandchildren = []
        for child in children:
            child_id = child["event_id"]
            for entry in all_events:
                if entry["event"].get("parent_event_id") == child_id:
                    grandchildren.append(entry["event"])

        self._json_response({
            "event": target,
            "parents": parents,      # ordered root → direct parent
            "children": children,     # direct children
            "grandchildren": grandchildren,  # grandchildren (2nd level)
        })

    def _handle_get_query(self, params):
        event_type = params.get("type", [None])[0]
        ctx_id = params.get("ctx_id", [None])[0]
        parent_event_id = params.get("parent_event_id", [None])[0]
        source = params.get("source", [None])[0]
        from_time = params.get("from", [None])[0]
        to_time = params.get("to", [None])[0]
        text = params.get("q", [None])[0]
        raw_limit = params.get("limit", [None])[0]
        if raw_limit is not None:
            try:
                limit_eff = min(int(raw_limit), MAX_QUERY_LIMIT)
            except (ValueError, TypeError):
                limit_eff = DEFAULT_QUERY_LIMIT
        else:
            # BIT-CHR.35 P3 — cap por defecto: una query GET sin filtros ya
            # no devuelve el ledger completo (43K eventos / ~50MB JSON).
            limit_eff = DEFAULT_QUERY_LIMIT
        results = query_events(
            self._ledger_path,
            event_type=event_type,
            ctx_id=ctx_id,
            parent_event_id=parent_event_id,
            source=source,
            from_time=from_time,
            to_time=to_time,
            text=text,
            limit=limit_eff,
        )
        self._json_response(results)

    def _handle_get_events(self, params):
        """GET /api/events — list all events with optional limit/offset."""
        try:
            limit = int(params.get("limit", [0])[0])
        except (ValueError, TypeError):
            limit = 0
        try:
            offset = int(params.get("offset", [0])[0])
        except (ValueError, TypeError):
            offset = 0

        entries = self._index.query()
        events = [entry["event"] for entry in entries]

        if offset > 0:
            events = events[offset:]
        if limit > 0:
            events = events[:limit]

        self._json_response(events)

    def _handle_check_update(self):
        from causadb._updater import check_update
        result = check_update()
        self._json_response(result)

    def _handle_install_update(self):
        """POST /api/update — trigger an update install (admin only).

        Delegates to :func:`causadb._updater.install_or_check` which
        checks for a new release and, if available, downloads, verifies
        and applies it. Mutates the running binary — requires ``admin``.
        """
        from causadb._updater import install_or_check
        try:
            result = install_or_check()
            self._json_response({"status": "ok", **result})
        except Exception as e:
            logging.exception("install_or_check failed")
            self._json_response({"status": "error", "error": str(e)}, 500)

    def _handle_score(self):
        from causadb._score import compute_score
        from causadb._config import CausaDBConfig
        config = CausaDBConfig(ledger_path=self._ledger_path)
        result = compute_score(self._ledger_path, config)
        self._json_response(result)

    # ── Crash endpoints ─────────────────────────────────────────────

    def _handle_get_crashes(self):
        """GET /api/crashes — return list of crash reports (requires ``query``)."""
        from causadb._crash_reporter import list_crashes
        crashes = list_crashes()
        # Strip full stack_text from API response (too large for dashboard)
        result = []
        for c in crashes:
            result.append({
                "crash_id": c["crash_id"],
                "timestamp": c["timestamp"],
                "exception_type": c["exception_type"],
                "exception_msg": c["exception_msg"],
                "os": c["os"],
                "version": c["version"],
                "occurrences": c["occurrences"],
            })
        self._json_response(result)

    def _handle_delete_crashes(self):
        """DELETE /api/crashes — delete all crash reports (requires ``admin``)."""
        from causadb._crash_reporter import delete_all_crashes
        count = delete_all_crashes()
        self._json_response({"status": "deleted", "count": count})

    def _handle_delete_crash(self, crash_id: str):
        """DELETE /api/crashes/{crash_id} — delete a specific crash (requires ``admin``)."""
        from causadb._crash_reporter import delete_crash
        if delete_crash(crash_id):
            self._json_response({"status": "deleted", "crash_id": crash_id})
        else:
            self._json_response({"error": "crash not found"}, 404)

    def _handle_export_crashes(self):
        """POST /api/crashes/export — export all crashes to a local file."""
        from causadb._crash_reporter import crashes_to_export_file
        try:
            path = crashes_to_export_file()
            self._json_response({"status": "exported", "path": path})
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    def _handle_assistant(self):
        """POST /api/assistant — query the local Ollama assistant.

        Public endpoint (no auth required) — runs on localhost only.
        Expects JSON body with ``{"question": "..."}``.
        Returns ``{"response": "..."}`` or ``{"error": "..."}`` with
        appropriate HTTP status code.
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body)
            question = data.get("question", "").strip()
            if not question:
                self._json_response({"error": "question is required"}, 400)
                return

            from causadb._assistant import Assistant
            assistant = Assistant()
            if not Assistant.is_ollama_running():
                self._json_response({
                    "error": "Ollama no está corriendo. Instalá Ollama y corré: ollama serve",
                    "hint": "Descargá Ollama de https://ollama.com y ejecutá: ollama pull smollm2:135m && ollama serve"
                }, 503)
                return

            response = assistant.ask(question)
            self._json_response({"response": response})

        except json.JSONDecodeError:
            self._json_response({"error": "Invalid JSON"}, 400)
        except Exception as e:
            self._json_response({"error": str(e)}, 500)

    # ── DELETE handler ──────────────────────────────────────────────

    def do_DELETE(self):
        """Handle DELETE requests — crash management only."""
        parsed = urlparse(self.path)

        if parsed.path == "/api/crashes":
            if not self._check_auth("admin"):
                return
            self._handle_delete_crashes()
        elif parsed.path.startswith("/api/crashes/"):
            if not self._check_auth("admin"):
                return
            crash_id = parsed.path.split("/")[-1]
            self._handle_delete_crash(crash_id)
        else:
            self._json_response({"error": "not found"}, 404)

    def _serve_dashboard_file(self, filename):
        """Serve a static file from the dashboard directory.

        Security: prevents directory traversal by resolving the real path
        and checking it stays within DASHBOARD_DIR.
        """
        # Prevent path traversal
        safe_filename = os.path.basename(filename)
        filepath = os.path.join(self.DASHBOARD_DIR, safe_filename)
        real_dashboard = os.path.realpath(self.DASHBOARD_DIR)
        real_path = os.path.realpath(filepath)

        if not real_path.startswith(real_dashboard):
            self._json_response({"error": "not found"}, 404)
            return

        if not os.path.isfile(real_path):
            self._json_response({"error": "not found"}, 404)
            return

        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(safe_filename)
        if mime_type is None:
            ext = os.path.splitext(safe_filename)[1].lower()
            mime_type = {
                ".html": "text/html",
                ".js": "application/javascript",
                ".css": "text/css",
                ".json": "application/json",
                ".png": "image/png",
                ".svg": "image/svg+xml",
            }.get(ext, "application/octet-stream")

        self.send_response(200)
        self.send_header("Content-Type", mime_type)
        self.end_headers()
        with open(real_path, "rb") as f:
            self.wfile.write(f.read())


def serve(ledger_path: str, host: str = "127.0.0.1", port: int = 7457, auth_manager=None, user_store=None, on_server_created=None):
    """Start the REST API server. Blocks until interrupted.

    Args:
        ledger_path: Absolute path to the ledger file.
        host: Bind address (default ``127.0.0.1``).
        port: TCP port (default ``7457``).
        auth_manager: Optional :class:`~causadb._auth.AuthManager` instance.
            When ``None`` or disabled, all requests pass without auth.
        user_store: Optional :class:`~causadb._user_store.UserStore` instance
            for persistent RBAC (#10).
        on_server_created: Optional callback ``f(server)`` invocado con la
            instancia del :class:`http.server.HTTPServer` justo después de
            crearla y ANTES de que el server empiece a atender
            (BIT-CHR.41: registrar el server + arrancar el
            daemon antes de bloquear).

    Nota de diseño: ``serve_forever()`` corre en un thread worker y el
    thread principal espera con ``join``. Motivo: el handler de SIGTERM
    (``install_signal_handlers``) corre en el thread principal y llama
    ``server.shutdown()``, que hace ``join()`` del thread de
    ``serve_forever`` — si ambos fueran el principal, el handler se
    juntaría a sí mismo (deadlock) y ``os._exit(0)`` nunca correría.
    """
    server = HTTPServer(
        (host, port),
        lambda *args, **kwargs: CausaDBAPIHandler(
            ledger_path, *args, auth_manager=auth_manager, user_store=user_store, **kwargs
        ),
    )
    if on_server_created is not None:
        on_server_created(server)
    logging.info(f"CausaDB REST API listening on {host}:{port}")
    from threading import Thread
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(timeout=1.0)
    except KeyboardInterrupt:
        server.shutdown()
        worker.join(timeout=5.0)


def serve_in_thread(ledger_path: str, host: str = "127.0.0.1", port: int = 7457, auth_manager=None, user_store=None):
    """Start the REST API server in a daemon thread. Returns the server.

    Args:
        ledger_path: Absolute path to the ledger file.
        host: Bind address (default ``127.0.0.1``).
        port: TCP port (default ``7457``).
        auth_manager: Optional :class:`~causadb._auth.AuthManager` instance.
            When ``None`` or disabled, all requests pass without auth.
        user_store: Optional :class:`~causadb._user_store.UserStore` instance
            for persistent RBAC (#10).

    Returns:
        The :class:`http.server.HTTPServer` instance (already started).
    """
    from threading import Thread
    server = HTTPServer(
        (host, port),
        lambda *args, **kwargs: CausaDBAPIHandler(
            ledger_path, *args, auth_manager=auth_manager, user_store=user_store, **kwargs
        ),
    )
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server
