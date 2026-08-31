import hashlib
import json
import os
import gzip
from causadb._validation_result import ValidationResult

class ReplayIntegrityError(Exception):
    pass

class LedgerValidator:
    def __init__(self, ledger_path: str):
        if not ledger_path:
            raise ValueError("ledger_path is required")
        self.ledger_path = ledger_path
        self.archive_dir = os.path.join(os.path.dirname(ledger_path), "archive")

    def _compute_hash(self, event_data: str, prev_hash: str) -> str:
        return hashlib.sha256((event_data + prev_hash).encode()).hexdigest()

    def _validate_from_lines(self, lines, start_index: int, expected_prev: str):
        entry_index = start_index - 1
        last_hash = expected_prev
        for line in lines:
            entry_index += 1
            try:
                entry = json.loads(line.strip())
            except json.JSONDecodeError:
                return ValidationResult(is_valid=False, failure_type="CORRUPTION",
                                        position=entry_index, description="Invalid JSON format")
            
            if entry.get("prev_hash") != last_hash:
                return ValidationResult(is_valid=False, failure_type="CONTINUITY_BREAK",
                                        position=entry_index,
                                        description=f"Expected {last_hash}")
            
            event_json = json.dumps(entry.get("event", {}), sort_keys=True)
            computed = self._compute_hash(event_json, entry.get("prev_hash", ""))
            if entry.get("hash") != computed:
                return ValidationResult(is_valid=False, failure_type="HASH_MISMATCH",
                                        position=entry_index)
            last_hash = entry.get("hash")
        return ValidationResult(is_valid=True, _last_hash=last_hash)

    def validate_chain(self) -> ValidationResult:
        expected_prev = "GENESIS"
        offset = 1

        if os.path.exists(self.archive_dir):
            archives = sorted([f for f in os.listdir(self.archive_dir) if f.endswith(".gz")])
            for archive in archives:
                with gzip.open(os.path.join(self.archive_dir, archive), "rt") as f:
                    lines = f.readlines()
                    result = self._validate_from_lines(lines, offset, expected_prev)
                    if not result.is_valid:
                        return result
                    expected_prev = result._last_hash
                    offset += len(lines)

        if os.path.exists(self.ledger_path) and os.path.getsize(self.ledger_path) > 0:
            with open(self.ledger_path, "r") as f:
                lines = f.readlines()
                return self._validate_from_lines(lines, offset, expected_prev)

        return ValidationResult(is_valid=True, _last_hash=expected_prev)

    def validate_or_raise(self):
        result = self.validate_chain()
        if not result.is_valid:
            raise ReplayIntegrityError(f"Ledger corruption: {result.failure_type} at position {result.position}"
                                       + (f" - {result.description}" if result.description else ""))
        return result
