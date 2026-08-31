import hmac
import hashlib
import re
from typing import Optional

NAMESPACE_REGEX = r"^[a-z][a-z0-9_-]*(:[a-z][a-z0-9_-]*)?$"
_NAMESPACE_RE = re.compile(NAMESPACE_REGEX)


def validate_source(source: str, source_type: str, expected_namespace: Optional[str] = None) -> bool:
    if not isinstance(source, str):
        return False
    if _NAMESPACE_RE.fullmatch(source) is None:
        return False
    if expected_namespace is not None:
        prefix = source.split(":", 1)[0]
        if prefix != expected_namespace:
            return False
    # humans do not sign with HMAC; namespace validation alone applies.
    # agents/llms satisfy the same namespace contract for MVP.
    return True


def sign_source(source: str, secret_key: str) -> str:
    digest = hmac.new(
        key=secret_key.encode("utf-8"),
        msg=source.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()
    return source + "." + digest[:16]


def verify_source(signed_source: str, secret_key: str) -> bool:
    # Fall-Closed: any malformed input → False.
    if not isinstance(signed_source, str):
        return False
    if signed_source.count(".") != 1:
        return False
    prefix, suffix = signed_source.split(".", 1)
    if not prefix or not suffix:
        return False
    expected = sign_source(prefix, secret_key)
    expected_suffix = expected.rsplit(".", 1)[1]
    return hmac.compare_digest(suffix, expected_suffix)
