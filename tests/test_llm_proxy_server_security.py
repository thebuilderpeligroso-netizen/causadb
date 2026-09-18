"""RED — seguridad proxy LLM: headers, redact-then-truncate, perms, auth, bind.

Specs auditadas:
1. FORWARD vs NEVER_LOG (auth solo sin clave server-side; headers jamás a ledger/capture).
2. redact PRIMERO con patrones existentes, truncar DESPUÉS; 3 campos, 2 sumideros.
3. Bind loopback por defecto; require_token con compare_digest + 401.
4. Capture 0600 + dir 0700.
Estos tests deben FALLAR antes del fix (RED) y PASAR después (GREEN).
"""

import json
import os
import stat
import threading
import time
from unittest.mock import patch

SECRET_SK = "sk-abcdefghijklmnopqrstuvw1234567890"
SECRET_BEARER_SUFFIX = "abcdefghij1234567890"
BEARER_SECRET = f"Bearer {SECRET_BEARER_SUFFIX}supersecret123"


def _read_ledger_events(ledger_path: str):
    events = []
    with open(ledger_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line)["event"])
            except (json.JSONDecodeError, KeyError):
                pass
    return events


def _start_server(cls_holder, server):
    t = threading.Thread(target=server.start, daemon=True)
    t.start()
    time.sleep(0.2)
    return t


class TestForwardVsNeverLog:
    def test_client_auth_not_forwarded_when_server_key_set(self, tmp_path, monkeypatch):
        """Con clave server-side en env, NO reenviar la del cliente; inyectar la del server."""
        from causadb._llm_proxy_server import LLMProxyServer

        monkeypatch.setenv("OPENAI_API_KEY", "sk-server-side-key-aaaaabbbbbcccccddddd")
        ledger = os.path.join(tmp_path, "ledger.log")
        capture = os.path.join(tmp_path, "capture.jsonl")
        mock_body = {
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        }
        server = LLMProxyServer(
            ledger_path=ledger, host="127.0.0.1", port=0, capture_path=capture,
        )
        port = server._server.server_address[1]
        t = _start_server(None, server)
        captured = {}
        import urllib.request as _ur_real
        _real_urlopen = _ur_real.urlopen

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(mock_body).encode()

        def _spy(req, timeout=60):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            # Solo interceptar upstream; el tráfico al proxy sigue real
            if "api.openai.com" not in url and "9999" not in url:
                return _real_urlopen(req, timeout=timeout)
            captured["headers"] = dict(req.header_items())
            # devolver upstream mock sin tocar red
            return _FakeResp()

        try:
            handler_cls = server._server.RequestHandlerClass
            # Parchear urlopen a nivel urllib para capturar headers reales del _forward
            with patch("urllib.request.urlopen", side_effect=_spy):
                import urllib.request as ur
                data = json.dumps({
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hola"}],
                }).encode()
                req = ur.Request(
                    f"http://127.0.0.1:{port}/openai/v1/chat/completions",
                    data=data,
                    headers={"Content-Type": "application/json",
                             "Authorization": f"Bearer {SECRET_SK}-client-leak"},
                )
                with ur.urlopen(req) as resp:
                    assert resp.status == 200
            time.sleep(0.2)
            hdrs_lower = {k.lower(): v for k, v in captured.get("headers", {}).items()}
            # la clave del cliente NO debe salir; la del server SÍ
            assert "sk-server-side-key" in (hdrs_lower.get("authorization", "") or "")
            assert "client-leak" not in json.dumps(hdrs_lower)
            # NEVER_LOG: ledger y capture jamás contienen auth del cliente
            raw_ledger = open(ledger).read() if os.path.exists(ledger) else ""
            raw_cap = open(capture).read() if os.path.exists(capture) else ""
            assert "client-leak" not in raw_ledger
            assert "client-leak" not in raw_cap
        finally:
            server.stop()
            t.join(timeout=3)

    def test_only_allowlisted_headers_forwarded(self, tmp_path):
        """Solo FORWARD list sale al upstream; X-Custom / Cookie jamás."""
        from causadb._llm_proxy_server import LLMProxyServer

        # sin clave server-side: auth del cliente sí puede reenviarse, pero extras no
        for v in ("OPENAI_API_KEY", "OPENAI_KEY"):
            if v in os.environ:
                del os.environ[v]
        ledger = os.path.join(tmp_path, "ledger.log")
        mock_body = {
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        }
        server = LLMProxyServer(ledger_path=ledger, host="127.0.0.1", port=0)
        port = server._server.server_address[1]
        t = _start_server(None, server)
        captured = {}
        import urllib.request as _ur_real2
        _real_urlopen2 = _ur_real2.urlopen

        class _FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(mock_body).encode()

        def _spy(req, timeout=60):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "api.openai.com" not in url and "9999" not in url:
                return _real_urlopen2(req, timeout=timeout)
            captured["headers"] = dict(req.header_items())
            return _FakeResp()

        try:
            with patch("urllib.request.urlopen", side_effect=_spy):
                import urllib.request as ur
                data = json.dumps({
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                }).encode()
                req = ur.Request(
                    f"http://127.0.0.1:{port}/openai/v1/chat/completions",
                    data=data,
                    headers={"Content-Type": "application/json",
                             "Accept-Language": "es-AR",
                             "X-Custom-Secret": "no-debe-salir",
                             "Cookie": "sid=abc"},
                )
                with ur.urlopen(req) as resp:
                    assert resp.status == 200
            hdrs_lower = {k.lower(): v for k, v in captured.get("headers", {}).items()}
            assert hdrs_lower.get("accept-language") == "es-AR"
            assert "x-custom-secret" not in hdrs_lower
            assert "cookie" not in hdrs_lower
        finally:
            server.stop()
            t.join(timeout=3)


class TestRedactThenTruncate:
    def test_secret_redacted_in_ledger_and_capture_tokens_intact(self, tmp_path):
        from causadb._llm_proxy_server import LLMProxyServer
        ledger = os.path.join(tmp_path, "ledger.log")
        capture = os.path.join(tmp_path, "capture.jsonl")
        mock_body = {
            "choices": [{"message": {"content": f"la clave es {SECRET_SK} fin"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 9},
        }
        server = LLMProxyServer(
            ledger_path=ledger, host="127.0.0.1", port=0, capture_path=capture,
        )
        port = server._server.server_address[1]
        t = _start_server(None, server)
        try:
            handler_cls = server._server.RequestHandlerClass
            with patch.object(handler_cls, "_forward", return_value=mock_body):
                import urllib.request as ur
                data = json.dumps({
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": f"mi secreto {SECRET_SK} aquí"}],
                }).encode()
                req = ur.Request(
                    f"http://127.0.0.1:{port}/openai/v1/chat/completions",
                    data=data, headers={"Content-Type": "application/json"},
                )
                with ur.urlopen(req) as resp:
                    assert resp.status == 200
            time.sleep(0.2)
            raw_ledger = open(ledger).read()
            raw_cap = open(capture).read()
            assert SECRET_SK not in raw_ledger, "secreto en claro en ledger"
            assert SECRET_SK not in raw_cap, "secreto en claro en capture"
            assert "***" in raw_ledger and "***" in raw_cap
            events = _read_ledger_events(ledger)
            llm = [e for e in events if e["event_type"] == "LLM_INVOKED"][0]
            assert llm["payload"]["tokens_in"] == 7
            assert llm["payload"]["tokens_out"] == 9
        finally:
            server.stop()
            t.join(timeout=3)

    def test_reasoning_secret_redacted(self, tmp_path):
        from causadb._llm_proxy_server import LLMProxyServer
        ledger = os.path.join(tmp_path, "ledger.log")
        capture = os.path.join(tmp_path, "capture.jsonl")
        reason_secret = f"pienso con {SECRET_SK} en mente"
        mock_body = {
            "choices": [{"message": {"content": "ok", "reasoning_content": reason_secret}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        }
        server = LLMProxyServer(
            ledger_path=ledger, host="127.0.0.1", port=0, capture_path=capture,
        )
        port = server._server.server_address[1]
        t = _start_server(None, server)
        try:
            handler_cls = server._server.RequestHandlerClass
            with patch.object(handler_cls, "_forward", return_value=mock_body):
                import urllib.request as ur
                data = json.dumps({
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hola"}],
                }).encode()
                req = ur.Request(
                    f"http://127.0.0.1:{port}/openai/v1/chat/completions",
                    data=data, headers={"Content-Type": "application/json"},
                )
                with ur.urlopen(req) as resp:
                    assert resp.status == 200
            time.sleep(0.2)
            raw_ledger = open(ledger).read()
            raw_cap = open(capture).read()
            assert SECRET_SK not in raw_ledger
            assert SECRET_SK not in raw_cap
            events = _read_ledger_events(ledger)
            rs = [e for e in events if e["event_type"] == "REASONING_STEP"]
            assert len(rs) >= 1
            assert SECRET_SK not in rs[0]["payload"]["reasoning"]
        finally:
            server.stop()
            t.join(timeout=3)

    def test_secret_crossing_2000_boundary_stays_redacted(self, tmp_path):
        """Redact ANTES de truncar: secreto que cruza el límite 2000 sigue tapado."""
        from causadb._llm_proxy_server import LLMProxyServer
        ledger = os.path.join(tmp_path, "ledger.log")
        capture = os.path.join(tmp_path, "capture.jsonl")
        # secreto a caballo del corte 2000: truncar-antes-de-redactar deja fragmento
        # "sk-..." que el redactor del writer ya no matchea (exige 20+ chars).
        # Redactar PRIMERO lo tapa entero (--> "***") y el corte posterior es seguro.
        prompt = "A" * 1990 + f" {SECRET_SK} " + "B" * 100
        mock_body = {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        }
        server = LLMProxyServer(
            ledger_path=ledger, host="127.0.0.1", port=0, capture_path=capture,
        )
        port = server._server.server_address[1]
        t = _start_server(None, server)
        try:
            handler_cls = server._server.RequestHandlerClass
            with patch.object(handler_cls, "_forward", return_value=mock_body):
                import urllib.request as ur
                data = json.dumps({
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": prompt}],
                }).encode()
                req = ur.Request(
                    f"http://127.0.0.1:{port}/openai/v1/chat/completions",
                    data=data, headers={"Content-Type": "application/json"},
                )
                with ur.urlopen(req) as resp:
                    assert resp.status == 200
            time.sleep(0.2)
            raw_ledger = open(ledger).read()
            assert SECRET_SK not in raw_ledger
            # Corte ingenuo (truncar sin redactar) deja "sk-..." parcial que el
            # writer ya no matchea: ni siquiera el prefijo puede filtrar.
            # El payload grande va a $blob → revisar también blobs + capture.
            raw_cap = open(capture).read() if os.path.exists(capture) else ""
            assert SECRET_SK not in raw_cap, "secreto cruzando 2000 en claro en capture"
            assert "sk-" not in raw_cap, "fragmento sk- en capture (redact debió taparlo antes del corte)"
            # Barrer blobs materializados (si los hay) buscando fragmentos
            for root, _dirs, files in os.walk(tmp_path):
                for fn in files:
                    fp = os.path.join(root, fn)
                    try:
                        data = open(fp, "r", errors="ignore").read()
                    except OSError:
                        continue
                    if fp == capture:
                        continue  # ya verificado arriba
                    # el ledger inline solo guarda hash; los blobs sí guardan payload
                    if "sk-abcdefgh" in data:
                        raise AssertionError(f"fragmento de secreto en {fp}")
        finally:
            server.stop()
            t.join(timeout=3)


class TestCapturePerms:
    def test_capture_file_is_0600(self, tmp_path):
        from causadb._llm_proxy_server import CaptureLogger
        path = os.path.join(tmp_path, "cap.jsonl")
        logger = CaptureLogger(path)
        logger.log({"model": "t", "x": 1})
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"capture perms {oct(mode)} != 0o600"


class TestBindAndToken:
    def test_bind_loopback_by_default(self, tmp_path):
        from causadb._llm_proxy_server import LLMProxyServer
        srv = LLMProxyServer(host="0.0.0.0", port=0)
        try:
            assert srv.host == "127.0.0.1", f"host remoto sin allow_remote: {srv.host}"
        finally:
            srv._server.server_close()
        srv2 = LLMProxyServer(host="0.0.0.0", port=0, allow_remote=True)
        try:
            assert srv2.host == "0.0.0.0"
        finally:
            srv2._server.server_close()

    def test_require_token_401(self, tmp_path):
        from causadb._llm_proxy_server import LLMProxyServer
        ledger = os.path.join(tmp_path, "ledger.log")
        mock_body = {
            "choices": [{"message": {"content": "OK"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        }
        server = LLMProxyServer(
            ledger_path=ledger, host="127.0.0.1", port=0, require_token="tok-secreto-123",
        )
        port = server._server.server_address[1]
        t = _start_server(None, server)
        try:
            import urllib.request as ur
            import urllib.error
            handler_cls = server._server.RequestHandlerClass
            with patch.object(handler_cls, "_forward", return_value=mock_body):
                data = json.dumps({
                    "model": "gpt-4o",
                    "messages": [{"role": "user", "content": "hi"}],
                }).encode()
                # sin token → 401
                req = ur.Request(
                    f"http://127.0.0.1:{port}/openai/v1/chat/completions",
                    data=data, headers={"Content-Type": "application/json"},
                )
                try:
                    ur.urlopen(req)
                    assert False, "debió dar 401 sin token"
                except ur.HTTPError as e:
                    assert e.code == 401
                # con token → 200
                req2 = ur.Request(
                    f"http://127.0.0.1:{port}/openai/v1/chat/completions",
                    data=data,
                    headers={"Content-Type": "application/json",
                             "X-Proxy-Token": "tok-secreto-123"},
                )
                with ur.urlopen(req2) as resp:
                    assert resp.status == 200
        finally:
            server.stop()
            t.join(timeout=3)
