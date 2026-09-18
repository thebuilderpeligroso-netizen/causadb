"""Nuevos tests de hardening (TDD RED→GREEN).

1. push sin secretos: el body enviado al hub no contiene valores en claro.
2. pull inválido no apendea: evento inválido → SyncError, ledger intacto, seq intacta.
3. sin assets no instala: release sin .sig/.pem → error claro, sin apply.
4. password por prompt (mock): cmd_user_add resuelve args.password or env or getpass.
"""
import io
import json
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from causadb._sync import SyncEngine, SyncError
from causadb._ledger_writer import LedgerWriter
from causadb._event_schema import CanonicalEvent, EventMetadata
from causadb._event_types import EventType


class FakeHTTPResponse:
    def __init__(self, body: bytes = b""):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return None


def _create_ledger(ledger_path, payload=None):
    w = LedgerWriter(ledger_path)
    genesis = CanonicalEvent(
        event_type=EventType.SYSTEM_BOOT, ctx_id="genesis",
        source="causadb:init", source_type="human",
        payload={"action": "init"},
        metadata=EventMetadata(trace_id="init", session_id="init"),
    )
    w.append(genesis)
    ev = CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="t",
        source="test:sync", payload=payload or {"path": "a.txt"},
        metadata=EventMetadata(session_id="s"),
    )
    w.append(ev)
    return w


def _lines(ledger_path):
    if not os.path.isfile(ledger_path):
        return []
    with open(ledger_path) as f:
        return [json.loads(l) for l in f if l.strip()]


# 1. push sin secretos (defensa en profundidad: el ledger puede traer
# eventos viejos en claro, previos a la redacción del writer, o líneas
# escritas a mano; el push debe redactar igual antes de json.dumps)
def test_push_redacts_secrets_before_send(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    _create_ledger(ledger, payload={"path": "a.txt"})
    # Línea cruda con secretos en claro, bypaseando la redacción del writer
    # (simula ledger viejo pre-redacción): seq 2, payload con secretos.
    raw_entry = {
        "event": {
            "event_id": "raw-plaintext-1", "event_type": "FILE_MODIFIED",
            "timestamp": "2026-01-01T00:00:00Z", "ctx_id": "t",
            "source": "test:sync", "parent_event_id": None,
            "source_type": "agent", "schema_version": "0.1.0",
            "payload": {"path": "b.txt", "api_key": "sk-secret-ABC123",
                        "password": "hunter2-super"},
            "metadata": None, "pre_snapshot": None, "post_snapshot": None,
            "sequence_number": 2,
        },
        "prev_hash": "x", "hash": "y",
    }
    with open(ledger, "a") as f:
        f.write(json.dumps(raw_entry, sort_keys=True) + "\n")
    eng = SyncEngine(ledger)
    eng.configure("http://hub:8080", "k")

    captured = {}

    def fake_urlopen(req, *a, **kw):
        captured["data"] = req.data  # bytes del POST /sync/push
        return FakeHTTPResponse(b'{"accepted": 1}')

    with patch("urllib.request.urlopen", fake_urlopen):
        eng.push()

    body = captured["data"].decode()
    assert "hunter2-super" not in body, "password en claro viajó al hub"
    assert "sk-secret-ABC123" not in body, "api_key en claro viajó al hub"


# 2. pull inválido no apendea ni avanza seq
def test_pull_invalid_event_raises_without_append(tmp_path):
    ledger = str(tmp_path / "ledger.log")
    _create_ledger(ledger)
    eng = SyncEngine(ledger)
    eng.configure("http://hub:8080", "k")
    before = _lines(ledger)
    seq_before = eng._load_state().get("last_synced_seq", 0)

    good = {
        "event": {
            "event_id": "good-1111", "event_type": "FILE_MODIFIED",
            "timestamp": "2026-07-30T12:00:00Z", "ctx_id": "r",
            "source": "remote:n", "parent_event_id": None,
            "source_type": "agent", "schema_version": "0.1.0",
            "payload": {"path": "ok.txt"}, "metadata": None,
            "pre_snapshot": None, "post_snapshot": None,
            "sequence_number": 50,
        },
        "prev_hash": "a", "hash": "b",
    }
    bad = {
        "event": {
            # sin event_id / ctx_id / source → inválido
            "event_type": "FILE_MODIFIED", "timestamp": "2026-07-30T12:00:00Z",
            "payload": {"path": "bad.txt"},
            "sequence_number": 51,
        },
        "prev_hash": "b", "hash": "c",
    }
    pull_body = json.dumps({"events": [good, bad], "last_seq": 51}).encode()

    def fake_urlopen(req, *a, **kw):
        return FakeHTTPResponse(pull_body)

    with patch("urllib.request.urlopen", fake_urlopen):
        with pytest.raises(SyncError):
            eng.pull()

    after = _lines(ledger)
    assert len(after) == len(before), "pull inválido apendió eventos parciales"
    assert eng._load_state().get("last_synced_seq", 0) == seq_before, "seq avanzó con pull inválido"


# 3. sin assets (.sig/.pem) no instala
def test_install_or_check_without_sig_assets_fails_closed(tmp_path, monkeypatch):
    import causadb._updater as upd

    monkeypatch.setattr(upd, "get_latest_release", lambda: {
        "tag_name": "v9.9.9",
        "assets": [{"name": "causadb", "browser_download_url": "http://x/causadb"}],
    })
    monkeypatch.setattr(upd, "get_current_version", lambda: "0.1.0")
    fake_bin = tmp_path / "causadb"
    fake_bin.write_bytes(b"fake")
    monkeypatch.setattr(upd, "download_update", lambda version: str(fake_bin))
    called = {"apply": False}
    monkeypatch.setattr(upd, "apply_update", lambda p: called.update(apply=True) or "bak")

    with pytest.raises(RuntimeError, match="(?i)signat|\\.sig|\\.pem"):
        upd.install_or_check(check_only=False)
    assert called["apply"] is False, "aplicó update sin firmas verificadas"


# 4. password por prompt (mock): args.password None → getpass
def test_user_add_password_via_prompt(tmp_path, monkeypatch):
    from causadb.cli import _cmd_user as cu
    from causadb._user_store import UserStore

    store = UserStore(str(tmp_path))
    monkeypatch.setattr(cu, "_get_user_store", lambda: (store, str(tmp_path)))
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "prompted-pw-123")

    args = SimpleNamespace(username="promptuser", password=None, role="member")
    rc, out = cu.cmd_user_add(args)
    assert rc == 0, f"cmd_user_add falló: {out}"
    data = json.loads(out)
    assert data["status"] == "created"
    assert "prompted-pw-123" not in out, "password en claro en output"
    # la password del prompt debe autenticar
    assert store.authenticate("promptuser", "prompted-pw-123")


def test_user_add_parser_password_not_required():
    from causadb.cli.main import build_parser

    p = build_parser()
    ns = p.parse_args(["user", "add", "--username", "bob"])
    assert ns.password is None, "--password sigue siendo required=True"
