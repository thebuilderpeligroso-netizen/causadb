from dataclasses import dataclass, field
from typing import Dict, Any, Optional
from types import MappingProxyType
from datetime import datetime
import uuid
from ._event_types import EventType

@dataclass(frozen=True)
class EventMetadata:
    """Free-form metadata attached to a `CanonicalEvent`.

    `priority` is a legacy field introduced by `migrate_v0_0_to_v0_1` in
    `_schema_version.py`: the genesis event (SYSTEM_BOOT, the only event with
    metadata in production ledgers) carries `metadata.priority`. It was never
    declared here, so `from_dict()` (`EventMetadata(**metadata)`) raised
    `TypeError` and broke `why`/`trace`/`bisect`/`explain` (BIT-CHR.35 P1).

    Benign wire-format side effect: `to_dict()` will now emit
    `"priority": null` for newly written events that carry metadata. This is
    a compatible additive change — `from_dict()` tolerates the key, and
    readers with the old dataclass simply reject it the same way they already
    reject the genesis event. No event replay semantics change.
    """
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    agent_name: Optional[str] = None
    reasoning: Optional[str] = None
    prompt: Optional[str] = None
    session_id: Optional[str] = None
    priority: Optional[str] = None

@dataclass(frozen=True)
class CanonicalEvent:
    event_type: EventType
    ctx_id: str
    source: str
    parent_event_id: Optional[str] = None
    source_type: str = "agent"
    schema_version: str = "0.1.0"
    payload: MappingProxyType = field(default_factory=lambda: MappingProxyType({}))
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    metadata: Optional[EventMetadata] = None
    pre_snapshot: Optional[str] = None
    post_snapshot: Optional[str] = None

    def __post_init__(self):
        from causadb._event_registry import is_registered
        if isinstance(self.event_type, str):
            try:
                object.__setattr__(self, "event_type", EventType(self.event_type))
            except ValueError:
                raise ValueError(f"event_type must be an EventType or a registered custom type, got {self.event_type!r}")
        if not isinstance(self.event_type, EventType):
            raise ValueError(f"event_type must be instance of EventType, got {type(self.event_type)}")
        if not self.ctx_id:
            raise ValueError("ctx_id is required")
        if not self.source:
            raise ValueError("source is required")
        if self.source_type not in ["human", "agent", "llm"]:
            raise ValueError("source_type must be human, agent, or llm")
        if not isinstance(self.payload, MappingProxyType):
            object.__setattr__(self, 'payload', MappingProxyType(self.payload))

    def to_dict(self):
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value,
            "timestamp": self.timestamp,
            "ctx_id": self.ctx_id,
            "source": self.source,
            "parent_event_id": self.parent_event_id,
            "source_type": self.source_type,
            "schema_version": self.schema_version,
            "payload": dict(self.payload),
            "metadata": self.metadata.__dict__ if self.metadata else None,
            "pre_snapshot": self.pre_snapshot,
            "post_snapshot": self.post_snapshot,
        }

    @classmethod
    def from_dict(cls, data):
        metadata = data.get("metadata")
        from causadb._event_registry import is_registered
        # Crear instancia sin pasar por __post_init__ para evitar validación estricta
        # al leer eventos antiguos.
        obj = cls.__new__(cls)
        object.__setattr__(obj, "event_id", data["event_id"])
        raw_type = data["event_type"]
        try:
            object.__setattr__(obj, "event_type", EventType(raw_type))
        except ValueError:
            object.__setattr__(obj, "event_type", raw_type)
        object.__setattr__(obj, "timestamp", data.get("timestamp", datetime.utcnow().isoformat() + "Z"))
        object.__setattr__(obj, "ctx_id", data["ctx_id"])
        object.__setattr__(obj, "source", data["source"])
        object.__setattr__(obj, "parent_event_id", data.get("parent_event_id"))
        object.__setattr__(obj, "source_type", data.get("source_type", "agent"))
        object.__setattr__(obj, "schema_version", data.get("schema_version", "0.1.0"))
        object.__setattr__(obj, "payload", MappingProxyType(data.get("payload", {})))
        object.__setattr__(obj, "metadata", EventMetadata(**metadata) if metadata else None)
        object.__setattr__(obj, "pre_snapshot", data.get("pre_snapshot"))
        object.__setattr__(obj, "post_snapshot", data.get("post_snapshot"))
        return obj
