"""Sistema de autenticación y roles para CausaDB REST API (I.2).

Roles:
    - "admin": puede hacer todo (register types, config, log, query, replay).
    - "member": puede log, query y replay; no puede register types ni config.
    - "auditor": solo query (read-only).

Por defecto sin auth (localhost). Auth se activa con ``enable()``, típicamente
vía el flag ``--auth`` en el daemon.

RBAC persistente (#10): cuando se configura un ``UserStore`` (via
``AuthManager.enable_with_user_store()``), las API keys se validan primero
contra el dict de dev-mode (``--auth key=role``) y después contra el
``UserStore``. Esto permite migración gradual de dev-mode a persistente.
"""

from typing import Optional


class AuthManager:
    """Authentication and authorization manager.

    Cuando ``enabled=False`` (default), ``authenticate()`` retorna un rol
    con permisos totales y ``authorize()`` siempre retorna ``True``.
    Esto permite operar sin auth en localhost sin cambios en el handler.

    Artículo IX: Fall-Closed. Si ``authenticate()`` retorna None, el handler
    responde 401. Si ``authorize()`` retorna False, responde 403.
    """

    ROLES = ("admin", "member", "auditor")

    ACTIONS = ("log", "query", "register_type", "config", "replay")

    # Permission matrix
    _PERMISSIONS = {
        "admin": {"log": True, "query": True, "register_type": True, "config": True, "replay": True},
        "member": {"log": True, "query": True, "register_type": False, "config": False, "replay": True},
        "auditor": {"log": False, "query": True, "register_type": False, "config": False, "replay": False},
    }

    def __init__(self, enabled: bool = False):
        self._enabled = enabled
        self._api_keys: dict[str, str] = {}
        self._user_store = None  # Optional UserStore instance

    @property
    def enabled(self) -> bool:
        """Whether auth enforcement is active."""
        return self._enabled

    @property
    def user_store(self):
        """Optional :class:`causadb._user_store.UserStore` instance."""
        return self._user_store

    def enable(self, api_keys: dict[str, str]) -> None:
        """Activate auth with a dict of ``{api_key: role}``.

        Args:
            api_keys: Mapping from API key string to role string.
                Each key must be at least 8 characters long.
                Each role must be one of ``admin``, ``member``, ``auditor``.

        Raises:
            ValueError: if any key is too short or any role is invalid.
        """
        for key, role in api_keys.items():
            if len(key) < 8:
                raise ValueError(
                    f"API key must be at least 8 characters: {key[:4]}..."
                )
            if role not in self.ROLES:
                raise ValueError(f"Invalid role '{role}'. Must be one of {self.ROLES}")
        self._api_keys = dict(api_keys)
        self._enabled = True

    def enable_with_user_store(self, config_dir: str, api_keys: Optional[dict[str, str]] = None) -> None:
        """Activate auth with a persistent UserStore.

        Args:
            config_dir: Path to the ``.causadb/`` directory that contains
                (or will contain) ``users.json``.
            api_keys: Optional dev-mode API keys dict for backward
                compatibility with ``--auth key=role``. These are checked
                *before* the UserStore.
        """
        from causadb._user_store import UserStore
        if api_keys:
            for key, role in api_keys.items():
                if role not in self.ROLES:
                    raise ValueError(
                        f"Invalid role '{role}'. Must be one of {self.ROLES}"
                    )
            self._api_keys = dict(api_keys)
        self._user_store = UserStore(config_dir)
        self._enabled = True

    def authenticate(self, api_key: Optional[str]) -> Optional[str]:
        """Validate an API key and return the associated role.

        Resolution order:
            1. Dev-mode keys (``--auth key=role``)
            2. UserStore (if configured)
            3. ``None`` (deny)

        Args:
            api_key: The ``X-API-Key`` header value, or ``None`` if absent.

        Returns:
            Role string (``admin``, ``member``, ``auditor``) if the key is
            valid, or ``None`` if authentication fails.

        When ``enabled=False``, always returns ``"admin"`` (full access).
        """
        if not self._enabled:
            return "admin"
        if not api_key:
            return None
        # 1. Dev-mode keys (fast path)
        role = self._api_keys.get(api_key)
        if role is not None:
            return role
        # 2. UserStore fallback
        if self._user_store is not None:
            user = self._user_store.get_user_by_api_key(api_key)
            if user is not None:
                return user["role"]
        # 3. Deny
        return None

    def authorize(self, role: str, action: str) -> bool:
        """Check whether *role* is allowed to perform *action*.

        Args:
            role: Role string returned by :meth:`authenticate`.
            action: One of ``log``, ``query``, ``register_type``, ``config``,
                ``replay``.

        Returns:
            ``True`` if the role is permitted, ``False`` otherwise.

        When ``enabled=False``, always returns ``True``.
        """
        if not self._enabled:
            return True
        row = self._PERMISSIONS.get(role, {})
        return row.get(action, False)
