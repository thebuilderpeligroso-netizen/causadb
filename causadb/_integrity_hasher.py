import hashlib
import json

class IntegrityHasher:
    @staticmethod
    def calculate_hash(data: dict) -> str:
        serialized = json.dumps(data, sort_keys=True).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()
