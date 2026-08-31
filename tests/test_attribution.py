import pytest
from causadb._attribution import (
    validate_source,
    sign_source,
    verify_source,
    NAMESPACE_REGEX,
)


def test_validate_source_valid_namespace():
    # Valid namespace + agent source_type → True.
    assert validate_source("opencode:agent1", "agent") is True


def test_validate_source_bare_name_accepted():
    # Bare names (without colon) are now valid — source_type classifies
    # the entity type; source is just the name.
    assert validate_source("bare_name", "agent") is True


def test_validate_source_invalid_wrong_namespace():
    # When expected_namespace is provided, the prefix before `:` must match.
    assert validate_source("claude-code:agent1", "agent", expected_namespace="opencode") is False


def test_sign_source_adds_hmac():
    # sign_source returns "source.<16-hex-chars>" suffix.
    secret_key = "supersecret"
    signed = sign_source("opencode:agent1", secret_key)
    assert isinstance(signed, str)
    assert signed.startswith("opencode:agent1.")
    suffix = signed.rsplit(".", 1)[1]
    # HMAC-SHA256 hexdigest[:16] → 16 hex chars
    assert len(suffix) == 16
    # All chars must be hex
    int(suffix, 16)


def test_verify_source_hmac():
    # Anti-teatro: must verify True for a valid signature AND False for a
    # tampered one. If verify_source is mutated to `return True`, the
    # tampered assertion fails.
    secret_key = "supersecret"
    signed = sign_source("opencode:agent1", secret_key)
    assert verify_source(signed, secret_key) is True

    # Tamper: flip a char in the suffix
    tampered_suffix = ("0" if signed[-1] != "0" else "1")
    tampered = signed[:-1] + tampered_suffix
    assert verify_source(tampered, secret_key) is False

    # Tamper: change the source prefix but keep the suffix
    prefix, suffix = signed.rsplit(".", 1)
    tampered_prefix = "opencode:agent2." + suffix
    assert verify_source(tampered_prefix, secret_key) is False

    # Malformed: no dot at all → False (Fall-Closed)
    assert verify_source("nodotstring", secret_key) is False

    # Malformed: multiple dots → False (exactly one dot required)
    assert verify_source("opencode:agent1.abc.def", secret_key) is False

    # Wrong key → False
    assert verify_source(signed, "wrongkey") is False


def test_human_source_no_hmac():
    # Humans do not sign with HMAC; namespace validation alone applies.
    assert validate_source("human:operator", "human") is True
