"""Tests for causadb/_secrets.py — Keyring for AI keys (item #7).

Test-First discipline (Article III): discriminatory tests that verify:
  - Never silent fail (Artículo IX)
  - Keyring first, env fallback
  - RuntimeError when keyring is unavailable for set/delete
"""

import os
import pytest

from causadb._secrets import Secrets, _keyring_available


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unavailable_keyring(monkeypatch):
    """Make keyring appear unavailable for the duration of a test."""
    monkeypatch.setattr("causadb._secrets._keyring_available", lambda: False)


def _mock_keyring_not_found(monkeypatch):
    """Make keyring available but return None for any password."""
    monkeypatch.setattr("causadb._secrets._keyring_available", lambda: True)
    import unittest.mock as mock
    fake_keyring = mock.MagicMock()
    fake_keyring.get_password.return_value = None
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)


# ---------------------------------------------------------------------------
# Test 1: get raises KeyError on missing secret (never silent fail)
# ---------------------------------------------------------------------------

def test_secrets_get_raises_on_missing():
    """Secrets.get('nonexistent_secret') raises KeyError when not in env or keyring.
    
    Anti-teatro (Artículo IX): never silently return empty string.
    """
    # Ensure no env var leaks
    for env_var in ["OPENAI_API_KEY", "OPENAI_KEY", "OPENAI_KEY_KEY",
                    "NONEXISTENT_SECRET", "NONEXISTENT_SECRET_KEY"]:
        os.environ.pop(env_var, None)
    
    with pytest.raises(KeyError) as exc_info:
        Secrets.get("nonexistent_secret")
    assert "nonexistent_secret" in str(exc_info.value)
    assert "not found" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Test 2: get returns env var value
# ---------------------------------------------------------------------------

def test_secrets_get_env_var(monkeypatch):
    """Secrets.get('openai_key') returns value from OPENAI_API_KEY env var."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-env-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # ensure no collision
    
    val = Secrets.get("openai_key")
    assert val == "sk-test-env-value"


def test_secrets_get_env_var_fallback_to_second_name(monkeypatch):
    """Secrets.get('anthropic_key') falls back to ANTHROPIC_KEY if ANTHROPIC_API_KEY not set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_KEY", "sk-anthropic-fallback")
    
    val = Secrets.get("anthropic_key")
    assert val == "sk-anthropic-fallback"


# ---------------------------------------------------------------------------
# Test 3: set raises RuntimeError without keyring
# ---------------------------------------------------------------------------

def test_secrets_set_raises_without_keyring(monkeypatch):
    """Secrets.set('x', 'y') raises RuntimeError when keyring is not available."""
    _unavailable_keyring(monkeypatch)
    
    with pytest.raises(RuntimeError) as exc_info:
        Secrets.set("test_key", "test_value")
    assert "keyring" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Test 4: get falls back to env after keyring fails
# ---------------------------------------------------------------------------

def test_secrets_get_falls_back_to_env_after_keyring_fail(monkeypatch):
    """When keyring raises, env var is used as fallback."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-keyring-failover")
    
    # Mock keyring to raise an exception
    monkeypatch.setattr("causadb._secrets._keyring_available", lambda: True)
    import unittest.mock as mock
    fake_keyring = mock.MagicMock()
    fake_keyring.get_password.side_effect = Exception("keyring unavailable")
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)
    
    val = Secrets.get("openai_key")
    assert val == "sk-keyring-failover"


# ---------------------------------------------------------------------------
# Test 5: has returns True for env var
# ---------------------------------------------------------------------------

def test_secrets_has_returns_true_for_env_var(monkeypatch):
    """Secrets.has('openai_key') returns True when env var is set."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-exists")
    
    assert Secrets.has("openai_key") is True


# ---------------------------------------------------------------------------
# Test 6: has returns False for missing secret
# ---------------------------------------------------------------------------

def test_secrets_has_returns_false_for_missing(monkeypatch):
    """Secrets.has returns False when neither env nor keyring has the secret."""
    for env_var in list(os.environ):
        if "API_KEY" in env_var or "KEY" in env_var:
            monkeypatch.delenv(env_var, raising=False)
    
    # Ensure keyring also returns nothing
    _mock_keyring_not_found(monkeypatch)
    
    assert Secrets.has("openai_key") is False


# ---------------------------------------------------------------------------
# Test 7: never silent fail (anti-teatro)
# ---------------------------------------------------------------------------

def test_anti_teatro_secrets_never_silent_fail():
    """Secrets.get always raises KeyError when secret not found — never returns ''.
    
    Anti-teatro (Artículo IX): the function must NEVER silently return an empty
    string. This test verifies the invariant for multiple key names.
    """
    # Clean env of any API key vars that might be set in the test environment
    for env_var in list(os.environ):
        if "API_KEY" in env_var or "_KEY" in env_var:
            os.environ.pop(env_var)
    
    for key_name in ["openai_key", "anthropic_key", "any_secret", "whatever_key", ""]:
        try:
            val = Secrets.get(key_name)
            # If we get here without KeyError, something is leaking from env
            # This would be a test environment issue — but we still assert the invariant
            assert val != "", (
                f"Secrets.get({key_name!r}) returned empty string — "
                f"violates Artículo IX (never silent fail)"
            )
        except KeyError:
            pass  # Expected: secret not found


# ---------------------------------------------------------------------------
# Additional: delete raises without keyring
# ---------------------------------------------------------------------------

def test_secrets_delete_raises_without_keyring(monkeypatch):
    """Secrets.delete('x') raises RuntimeError when keyring is not available."""
    _unavailable_keyring(monkeypatch)
    
    with pytest.raises(RuntimeError) as exc_info:
        Secrets.delete("test_key")
    assert "keyring" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# Additional: set/get round-trip with mocked keyring
# ---------------------------------------------------------------------------

def test_secrets_set_and_get_with_mocked_keyring(monkeypatch):
    """Secrets.set + Secrets.get round-trips via mocked keyring."""
    monkeypatch.setattr("causadb._secrets._keyring_available", lambda: True)
    import unittest.mock as mock
    fake_keyring = mock.MagicMock()
    
    stored = {}
    def fake_set_password(service, key, value):
        stored[(service, key)] = value
    def fake_get_password(service, key):
        return stored.get((service, key))
    
    fake_keyring.set_password = fake_set_password
    fake_keyring.get_password = fake_get_password
    fake_keyring.delete_password = lambda service, key: stored.pop((service, key), None)
    monkeypatch.setitem(__import__("sys").modules, "keyring", fake_keyring)
    
    Secrets.set("openai_key", "sk-roundtrip-test")
    val = Secrets.get("openai_key")
    assert val == "sk-roundtrip-test"
