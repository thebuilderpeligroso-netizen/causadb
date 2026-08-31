from dataclasses import dataclass
from typing import Optional

@dataclass
class ValidationResult:
    is_valid: bool
    failure_type: Optional[str] = None
    position: Optional[int] = None
    description: Optional[str] = None
    _last_hash: Optional[str] = None

    def __bool__(self):
        return self.is_valid
