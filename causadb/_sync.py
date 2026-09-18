"""Ledger federation engine. Hub-and-spoke, append-only, conflict-free.

Each node tracks its ``last_synced_seq`` in a ``sync_state.json`` file
co-located with the ledger. Push sends local events to the hub; pull
retrieves remote events. Because the ledger is append-only there are no
conflicts — no CRDT needed.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import os
import socket
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SYNC_STATE_FILENAME = "sync_state.json"

# Hub API key en Secrets (keyring/env), no en claro en el JSON.
# Migración gradual: configure() sigue persistiendo el legacy ``api_key``
# (compat con tests/estados viejos); _resolve_api_key() lo mueve a Secrets
# y lo borra del JSON en el próximo push/pull.
SYNC_HUB_SECRET_KEY = "sync_hub_api_key"


class SyncError(Exception):
    """Base error for sync operations."""


class SyncEngine:
    """Sync engine that pushes/pulls ledger events via REST API.

    Hub endpoint contract (see ``docs/sync_hub_api.md``):

      ``POST /sync/push``  — send local events to hub
      ``GET /sync/pull?last_seq=N&node_id=X``  — fetch remote events
    """

    def __init__(self, ledger_path: str, config_dir: Optional[str] = None):
        self.ledger_path = ledger_path
        self.config_dir = config_dir or os.path.dirname(ledger_path)

    # ------------------------------------------------------------------
    # State persistence  (sync_state.json)
    # ------------------------------------------------------------------

    def _state_path(self) -> str:
        return os.path.join(self.config_dir, SYNC_STATE_FILENAME)

    def _load_state(self) -> dict:
        path = self._state_path()
        if not os.path.isfile(path):
            return {
                "hub_url": "",
                "api_key": "",
                "last_synced_seq": 0,
                "interval_minutes": 60,
            }
        with open(path) as f:
            return json.load(f)

    def _save_state(self, state: dict):
        path = self._state_path()
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def configure(
        self,
        hub_url: str,
        api_key: str,
        interval_minutes: int = 60,
    ):
        """Configure sync settings and persist to disk."""
        state = self._load_state()
        state["hub_url"] = hub_url.rstrip("/")
        state["api_key"] = api_key
        state["interval_minutes"] = interval_minutes
        self._save_state(state)
        # Mejor esfuerzo: también guardar en Secrets (keyring/env).
        # El JSON legacy se limpia en _resolve_api_key() (próximo push/pull).
        try:
            from causadb._secrets import Secrets
            if api_key:
                Secrets.set(SYNC_HUB_SECRET_KEY, api_key)
        except Exception:
            pass

    def _resolve_api_key(self, state: dict) -> str:
        """Hub API key: Secrets primero, legacy JSON después.

        Si la clave vive aún en el JSON, se migra a Secrets (mejor
        esfuerzo) y se borra del archivo. Nunca retorna None.
        """
        try:
            from causadb._secrets import Secrets
        except ImportError:
            return state.get("api_key", "") or ""
        try:
            val = Secrets.get(SYNC_HUB_SECRET_KEY)
            if val:
                if state.get("api_key"):
                    state.pop("api_key", None)
                    try:
                        self._save_state(state)
                    except Exception:
                        pass
                return val
        except KeyError:
            pass
        except Exception:
            pass
        legacy = state.get("api_key", "") or ""
        if legacy:
            try:
                Secrets.set(SYNC_HUB_SECRET_KEY, legacy)
            except Exception:
                return legacy
            state.pop("api_key", None)
            try:
                self._save_state(state)
            except Exception:
                pass
        return legacy

    def get_config(self) -> dict:
        """Get current sync config (``api_key`` is masked for safety)."""
        state = self._load_state()
        try:
            from causadb._secrets import Secrets
            has_key = Secrets.has(SYNC_HUB_SECRET_KEY) or bool(state.get("api_key", ""))
        except Exception:
            has_key = bool(state.get("api_key", ""))
        return {
            "hub_url": state.get("hub_url", ""),
            "has_api_key": has_key,
            "last_synced_seq": state.get("last_synced_seq", 0),
            "interval_minutes": state.get("interval_minutes", 60),
            "ledger_path": self.ledger_path,
        }

    # ------------------------------------------------------------------
    # Ledger reading helpers
    # ------------------------------------------------------------------

    def _read_events_from(self, sequence: int) -> List[dict]:
        """Read ledger entries with ``sequence_number > sequence``.

        Sequence numbers are 1-indexed (genesis = 0, first event = 1).
        """
        from causadb._ledger_reader import LedgerReader

        try:
            reader = LedgerReader(self.ledger_path)
            all_entries = reader.read_all_entries()
        except Exception as e:
            raise SyncError(f"Failed to read ledger: {e}")

        events: List[dict] = []
        for entry in all_entries:
            ev = entry.get("event", {})
            seq = ev.get("sequence_number", 0)
            if seq > sequence:
                events.append(entry)
        return events

    def _get_last_sequence(self) -> int:
        """Return the last sequence number in the local ledger (0 if empty)."""
        from causadb._ledger_reader import LedgerReader

        try:
            reader = LedgerReader(self.ledger_path)
            entries = list(reader.read_all_entries())
            if not entries:
                return 0
            return entries[-1].get("event", {}).get("sequence_number", 0)
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Push / Pull
    # ------------------------------------------------------------------

    def _redact_events_for_push(self, events: List[dict]) -> List[dict]:
        """Redacta payloads antes de enviar al hub (defensa en profundidad).

        El writer ya redacta al escribir, pero el ledger puede traer eventos
        viejos en claro (pre-redacción). Nunca enviar secretos en claro.
        """
        try:
            from causadb._redactor import redact_payload
            from causadb._config import CausaDBConfig
        except ImportError:
            return events
        try:
            config = CausaDBConfig.from_env_with_overrides(self.ledger_path)
        except Exception:
            return events
        import copy
        out: List[dict] = []
        for entry in events:
            try:
                e = copy.deepcopy(entry)
                ev = e.get("event")
                if isinstance(ev, dict) and isinstance(ev.get("payload"), dict):
                    ev["payload"] = redact_payload(dict(ev["payload"]), config)
                out.append(e)
            except Exception:
                out.append(entry)
        return out

    def push(self) -> dict:
        """Push new local events to the hub.

        Returns:
            dict with ``pushed`` (count), ``last_seq``, ``hub_response``.

        Raises:
            SyncError if hub is unreachable or misconfigured.
        """
        state = self._load_state()
        hub_url = state.get("hub_url", "")
        api_key = self._resolve_api_key(state)

        if not hub_url:
            raise SyncError(
                "Hub URL not configured. Run: causadb sync config "
                "--hub-url <url> --api-key <key>"
            )

        last_seq: int = state.get("last_synced_seq", 0)
        events = self._read_events_from(last_seq)

        if not events:
            return {"pushed": 0, "last_seq": last_seq, "status": "no_new_events"}

        url = f"{hub_url}/sync/push"
        redacted_events = self._redact_events_for_push(events)
        payload = json.dumps({
            "events": redacted_events,
            "last_seq": last_seq,
            "node_id": self._get_node_id(),
        }).encode()

        try:
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": api_key,
                },
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise SyncError(f"Hub push failed (HTTP {e.code}): {body[:200]}")
        except urllib.error.URLError as e:
            raise SyncError(f"Hub connection failed: {e.reason}")
        except (json.JSONDecodeError, OSError) as e:
            raise SyncError(f"Hub response error: {e}")

        new_seq = self._get_last_sequence()
        state["last_synced_seq"] = new_seq
        self._save_state(state)

        return {
            "pushed": len(events),
            "last_seq": new_seq,
            "hub_response": result,
        }

    def pull(self) -> dict:
        """Pull new remote events from the hub and append them locally.

        Returns:
            dict with ``pulled`` (count), ``last_seq``, ``total_remote``.

        Raises:
            SyncError if hub is unreachable or misconfigured.
        """
        state = self._load_state()
        hub_url = state.get("hub_url", "")
        api_key = self._resolve_api_key(state)

        if not hub_url:
            raise SyncError(
                "Hub URL not configured. Run: causadb sync config "
                "--hub-url <url> --api-key <key>"
            )

        last_seq: int = state.get("last_synced_seq", 0)
        url = f"{hub_url}/sync/pull?last_seq={last_seq}&node_id={self._get_node_id()}"

        try:
            req = urllib.request.Request(url, headers={"X-API-Key": api_key})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else ""
            raise SyncError(f"Hub pull failed (HTTP {e.code}): {body[:200]}")
        except urllib.error.URLError as e:
            raise SyncError(f"Hub connection failed: {e.reason}")
        except (json.JSONDecodeError, OSError) as e:
            raise SyncError(f"Hub response error: {e}")

        remote_events: List[dict] = data.get("events", [])
        imported = 0
        last_seq_remote = last_seq

        if remote_events:
            from causadb._event_schema import CanonicalEvent
            from causadb._ledger_writer import LedgerWriter

            # Validar TODOS los eventos antes de apendar nada (atómico).
            # Un evento inválido aborta el pull completo: SyncError sin
            # append parcial ni avance de seq (fail-closed, Art. IX).
            parsed: List[Tuple[Any, dict]] = []
            for entry in remote_events:
                ev_data = entry.get("event", entry)
                if not isinstance(ev_data, dict):
                    raise SyncError(
                        "Failed to import events: invalid event entry"
                    )
                try:
                    event = CanonicalEvent.from_dict(ev_data)
                except Exception as e:
                    raise SyncError(
                        f"Failed to import events: invalid event: {e}"
                    )
                parsed.append((event, ev_data))

            try:
                writer = LedgerWriter(self.ledger_path)
                for event, ev_data in parsed:
                    writer.append(event)
                    imported += 1
                    seq = ev_data.get("sequence_number", 0)
                    if seq > last_seq_remote:
                        last_seq_remote = seq
            except Exception as e:
                raise SyncError(f"Failed to import events: {e}")

        if last_seq_remote > last_seq:
            state["last_synced_seq"] = last_seq_remote
            self._save_state(state)

        return {
            "pulled": imported,
            "last_seq": last_seq_remote,
            "total_remote": len(remote_events),
        }

    def full_sync(self) -> dict:
        """Push local changes then pull remote changes.

        Returns:
            Combined dict with ``push``, ``pull``, and ``timestamp`` keys.
        """
        push_result = self.push()
        pull_result = self.pull()
        return {
            "push": push_result,
            "pull": pull_result,
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
        }

    # ------------------------------------------------------------------
    # Node identity
    # ------------------------------------------------------------------

    @staticmethod
    def _get_node_id() -> str:
        """Return a stable node identifier derived from the hostname."""
        host = socket.gethostname()
        node_hash = hashlib.sha256(host.encode()).hexdigest()[:12]
        return f"node-{node_hash}"
