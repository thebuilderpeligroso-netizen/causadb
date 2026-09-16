"""Redacción de secretos — Fase 1 "No más secretos en claro".

Forward-only: SIN purga ni reescritura histórica. Los eventos ya
persistidos NO se reescriben (rompería la hash chain del ledger).
Esta redacción aplica solo hacia adelante (nuevos eventos/blobs).
"""
import hashlib
import re
from typing import Any, Set
from urllib.parse import urlsplit, urlunsplit
from causadb._config import CausaDBConfig

SENSITIVE_FIELDS: Set[str] = {
    "password", "api_key", "token", "secret", "credential", "private_key",
    "apikey", "passwd", "auth", "authorization", "access_token", "client_secret",
}

# Campos cuyo valor es una URL que puede contener credenciales embebidas
# (user:pass@host). H2.0: base_url / api_base_url no deben filtrar secretos
# (Art. V). Solo se aplica a claves con sufijo *_url / base_url.
URL_FIELDS: Set[str] = {"base_url", "api_base_url", "url", "api_url", "endpoint"}

_MAX_DEPTH = 10

# Regex de valores SOLO alta precisión, precompilados a nivel módulo.
_PRIVATE_KEY_RE = re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")
_AWS_RE = re.compile(r"AKIA[0-9A-Z]{16}")
_GHP_RE = re.compile(r"ghp_[A-Za-z0-9]{36}")
_SLACK_RE = re.compile(r"xox[baprs]-[A-Za-z0-9-]+")
_SK_RE = re.compile(r"sk-[A-Za-z0-9\-_]{20,}")
_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{10,}")

_VALUE_PATTERNS = (_PRIVATE_KEY_RE, _AWS_RE, _GHP_RE, _SLACK_RE, _SK_RE, _BEARER_RE)

_ALREADY_REDACTED_RE = re.compile(r"^[0-9a-f]{16}$")


def _redact_url_credentials(value: str) -> str:
    """Enmascara credenciales embebidas en una URL (user:pass@host)."""
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if parts.username is None:
        return value
    netloc = parts.netloc
    if "@" not in netloc:
        return value
    host_part = netloc.rsplit("@", 1)[-1]
    return urlunsplit((parts.scheme, "***@" + host_part, parts.path, parts.query, parts.fragment))


def _redact_str_value(value: str) -> str:
    """Aplica regex de alta precisión sobre un string libre."""
    out = value
    for pat in _VALUE_PATTERNS:
        out = pat.sub("***", out)
    return out


def _redact_any(value: Any, depth: int) -> Any:
    if depth > _MAX_DEPTH:
        return "***"
    if isinstance(value, dict):
        result = {}
        for key, item in dict(value).items():
            lk = key.lower() if isinstance(key, str) else key
            if isinstance(key, str) and lk in SENSITIVE_FIELDS:
                # Idempotencia anti-doble-hash (choke point put_redacted):
                # si ya es hash hex de 16, no re-hashear.
                if isinstance(item, str) and _ALREADY_REDACTED_RE.match(item):
                    result[key] = item
                else:
                    val_str = str(item)
                    result[key] = hashlib.sha256(val_str.encode()).hexdigest()[:16]
            elif isinstance(key, str) and lk in URL_FIELDS and isinstance(item, str):
                result[key] = _redact_url_credentials(_redact_str_value(item))
            else:
                result[key] = _redact_any(item, depth + 1)
        return result
    if isinstance(value, list):
        return [_redact_any(v, depth + 1) for v in value]
    if isinstance(value, str):
        return _redact_str_value(value)
    return value


def redact_payload(payload: dict, config: CausaDBConfig) -> dict:
    if not config.redaction_enabled:
        return dict(payload)

    return _redact_any(dict(payload), 0)


def put_redacted(blob_store, data: dict, config=None) -> str:
    """Choke point central: redacta antes de persistir en BlobStore.

    Si *config* trae ``redaction_enabled=False``, persiste tal cual
    (respeta opt-out). Si *config* es None, redacta por defecto
    (fail-closed hacia adelante).
    """
    enabled = True
    if config is not None:
        enabled = bool(getattr(config, "redaction_enabled", True))
    if not enabled:
        return blob_store.put(data)
    if config is None:
        from types import SimpleNamespace
        config = SimpleNamespace(redaction_enabled=True)
    redacted = redact_payload(dict(data), config)
    return blob_store.put(redacted)
