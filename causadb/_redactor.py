import hashlib
from typing import Set
from urllib.parse import urlsplit, urlunsplit
from causadb._config import CausaDBConfig

SENSITIVE_FIELDS: Set[str] = {"password", "api_key", "token", "secret", "credential", "private_key"}

# Campos cuyo valor es una URL que puede contener credenciales embebidas
# (user:pass@host). H2.0: base_url / api_base_url no deben filtrar secretos
# (Art. V). Solo se aplica a claves con sufijo *_url / base_url.
URL_FIELDS: Set[str] = {"base_url", "api_base_url", "url", "api_url", "endpoint"}


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


def redact_payload(payload: dict, config: CausaDBConfig) -> dict:
    if not config.redaction_enabled:
        return dict(payload)
    
    result = {}
    for key, value in dict(payload).items():
        if key.lower() in SENSITIVE_FIELDS:
            val_str = str(value)
            result[key] = hashlib.sha256(val_str.encode()).hexdigest()[:16]
        elif key.lower() in URL_FIELDS and isinstance(value, str):
            result[key] = _redact_url_credentials(value)
        else:
            result[key] = value
    return result