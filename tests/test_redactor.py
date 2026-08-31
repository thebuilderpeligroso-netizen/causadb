import pytest
import hashlib
from causadb._redactor import redact_payload
from causadb._config import CausaDBConfig
import causadb._redactor

@pytest.fixture
def config():
    return CausaDBConfig(ledger_path="/tmp/test.log")

def test_redact_password_field(config):
    payload = {"password": "secret123"}
    redacted = redact_payload(payload, config)
    assert redacted["password"] != "secret123"
    assert len(redacted["password"]) == 16

def test_redact_api_key_field(config):
    payload = {"api_key": "key123"}
    redacted = redact_payload(payload, config)
    assert redacted["api_key"] != "key123"

def test_redact_token_field(config):
    payload = {"token": "token123"}
    redacted = redact_payload(payload, config)
    assert redacted["token"] != "token123"

def test_redact_secret_field(config):
    payload = {"secret": "secret123"}
    redacted = redact_payload(payload, config)
    assert redacted["secret"] != "secret123"

def test_redact_preserves_non_sensitive_fields(config):
    payload = {"path": "/etc/foo", "action": "modify"}
    redacted = redact_payload(payload, config)
    assert redacted == payload

def test_redact_disabled_by_config():
    config = CausaDBConfig(ledger_path="/tmp/test.log", redaction_enabled=False)
    payload = {"password": "secret123"}
    redacted = redact_payload(payload, config)
    assert redacted == payload

def test_redact_is_one_way(config):
    """Artículo IX: el test debe valer algo. Verifica que la redacción
    es SHA256 (hex 16 chars, prefix del digest) y que no existe función
    inversa en el módulo."""
    payload = {"password": "secret123"}
    redacted = redact_payload(payload, config)
    # 1. El valor redactado debe ser el prefix de sha256 del valor original
    expected = hashlib.sha256(b"secret123").hexdigest()[:16]
    assert redacted["password"] == expected, (
        f"Expected sha256 prefix {expected}, got {redacted['password']}"
    )
    # 2. El valor redactado debe ser hex válido (no un placeholder)
    int(redacted["password"], 16)  # raise ValueError si no es hex
    # 3. No existe función inversa en el módulo (one-way intencional)
    assert not hasattr(causadb._redactor, "unredact_payload"), (
        "Redactor no debe exponer función de desredacción (one-way por diseño)"
    )
    assert not hasattr(causadb._redactor, "decrypt_payload"), (
        "Redactor no debe exponer función de desencriptación (no es AES-GCM)"
    )
