import logging
import json
import os
import threading
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class EventTypeSpec:
    required_fields: set
    enum_rules: dict = field(default_factory=dict)

_registry: dict[str, EventTypeSpec] = {}
_builtin_names: set[str] = set()
_lock = threading.Lock()

def register_type(name: str, spec: EventTypeSpec, builtin: bool = False) -> None:
    with _lock:
        _registry[name] = spec
        if builtin:
            _builtin_names.add(name)

def is_registered(name) -> bool:
    from causadb._event_types import EventType
    if isinstance(name, EventType):
        name = name.value
    with _lock:
        return name in _registry

def is_builtin(name: str) -> bool:
    return name in _builtin_names

def get_spec(name: str) -> Optional[EventTypeSpec]:
    with _lock:
        return _registry.get(name)

def get_all_schema_rules() -> dict:
    """Merge built-in SCHEMA_RULES with custom types from registry."""
    from causadb._schema_validator import SCHEMA_RULES
    rules = {k.value: v for k, v in SCHEMA_RULES.items()}
    with _lock:
        for name, spec in _registry.items():
            if name not in rules:
                rules[name] = spec.required_fields
    return rules

def load_from_config(config_path: str) -> int:
    """Carga custom_event_types desde config.json. Degradación suave: si falla, warning."""
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
        custom = config.get("custom_event_types", {})
        count = 0
        for name, spec_dict in custom.items():
            spec = EventTypeSpec(
                required_fields=set(spec_dict.get("required_fields", [])),
                enum_rules=spec_dict.get("enum_rules", {}),
            )
            register_type(name, spec)
            count += 1
        return count
    except (json.JSONDecodeError, OSError, KeyError) as e:
        logging.warning(f"Failed to load custom event types: {e}")
        return 0

def list_registered() -> list[str]:
    with _lock:
        return list(_registry.keys())
