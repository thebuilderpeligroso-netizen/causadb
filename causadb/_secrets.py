"""Secure secret storage using system keyring. Zero dependencies on keyring
when keyring is not installed — falls back gracefully to environment variables.

Usage::

    from causadb._secrets import Secrets

    # Get a secret (keyring > env var > KeyError)
    api_key = Secrets.get("openai_key")

    # Store a secret in the system keyring
    Secrets.set("openai_key", "sk-...")

    # Delete a secret from the system keyring
    Secrets.delete("openai_key")

    # Check if a secret exists
    if Secrets.has("openai_key"):
        ...
"""

import os
import logging

logger = logging.getLogger(__name__)

# Service name used as the "service" / "application" name in the system keyring.
SERVICE_NAME = "causadb"


def _keyring_available() -> bool:
    """Check if keyring module is available (optional dependency)."""
    try:
        import keyring  # noqa: F401
        return True
    except ImportError:
        return False


class Secrets:
    """Secret storage with system keyring backend + environment variable fallback.

    Convention (env var naming):
      - ``Secrets.get("openai_key")`` checks ``OPENAI_API_KEY`` (or ``OPENAI_KEY``)
      - ``Secrets.get("anthropic_key")`` checks ``ANTHROPIC_API_KEY`` (or ``ANTHROPIC_KEY``)

    Anti-pattern note (Artículo IX): this class NEVER silently fails. If the
    secret is not found in either keyring or environment, it raises ``KeyError``.
    No default empty string is returned.
    """

    # Mapping from our internal key names to the conventional env var names
    _ENV_MAP = {
        "openai_key": ("OPENAI_API_KEY", "OPENAI_KEY"),
        "anthropic_key": ("ANTHROPIC_API_KEY", "ANTHROPIC_KEY"),
    }

    @staticmethod
    def get(key: str) -> str:
        """Get a secret. Tries keyring first, then environment variable.

        Args:
            key: Secret identifier (e.g. ``"openai_key"``, ``"anthropic_key"``).

        Returns:
            The secret value as a string.

        Raises:
            KeyError: if the secret is not found in either keyring or environment.
        """
        # 1. Try the system keyring first
        if _keyring_available():
            try:
                import keyring
                val = keyring.get_password(SERVICE_NAME, key)
                if val:
                    return val
            except Exception as e:
                logger.debug("keyring.get_password(%r, %r) failed: %s",
                             SERVICE_NAME, key, e)

        # 2. Fall back to environment variables (conventional names)
        env_names = Secrets._ENV_MAP.get(key, ())
        for env_var in env_names:
            val = os.environ.get(env_var)
            if val:
                return val

        # 3. Generic env var guess: UPPER_CASE with _KEY suffix
        env_key = key.upper().replace("-", "_")
        if not env_key.endswith("_KEY"):
            env_key = env_key + "_KEY"
        val = os.environ.get(env_key)
        if val:
            return val

        # 4. Also try raw key uppercase as env var
        val = os.environ.get(key.upper().replace("-", "_"))
        if val:
            return val

        raise KeyError(
            f"Secret '{key}' not found in system keyring or environment. "
            f"Set it with `causadb config set {key} <value>` "
            f"or export the {env_names[0] if env_names else key.upper() + '_KEY'} env var."
        )

    @staticmethod
    def set(key: str, value: str) -> None:
        """Store a secret in the system keyring.

        Args:
            key: Secret identifier (e.g. ``"openai_key"``).
            value: The secret value to store.

        Raises:
            RuntimeError: if the ``keyring`` library is not installed.
        """
        if not _keyring_available():
            raise RuntimeError(
                "keyring library is required for secure storage. "
                "Install: pip install causadb[commercial]"
            )
        import keyring
        keyring.set_password(SERVICE_NAME, key, value)

    @staticmethod
    def delete(key: str) -> None:
        """Delete a secret from the system keyring.

        Args:
            key: Secret identifier to delete.

        Raises:
            KeyError: if the secret is not found in the keyring.
            RuntimeError: if the ``keyring`` library is not installed.
        """
        if not _keyring_available():
            raise RuntimeError(
                "keyring library is required for secure storage. "
                "Install: pip install causadb[commercial]"
            )
        import keyring
        try:
            keyring.delete_password(SERVICE_NAME, key)
        except keyring.errors.PasswordDeleteError:
            raise KeyError(
                f"Secret '{key}' not found in keyring. "
                f"Use `causadb config set {key} <value>` to set it first."
            )

    @staticmethod
    def has(key: str) -> bool:
        """Check if a secret exists in either keyring or environment.

        Args:
            key: Secret identifier to check.

        Returns:
            ``True`` if the secret exists, ``False`` otherwise.
        """
        try:
            Secrets.get(key)
            return True
        except KeyError:
            return False
