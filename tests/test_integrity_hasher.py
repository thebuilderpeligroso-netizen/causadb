import pytest
from causadb._integrity_hasher import IntegrityHasher

def test_calculate_hash_deterministic():
    data = {"a": 1, "b": 2}
    assert IntegrityHasher.calculate_hash(data) == IntegrityHasher.calculate_hash(data)

def test_calculate_hash_sorts_keys():
    assert IntegrityHasher.calculate_hash({"a": 1, "b": 2}) == IntegrityHasher.calculate_hash({"b": 2, "a": 1})

def test_calculate_hash_different_input():
    assert IntegrityHasher.calculate_hash({"a": 1}) != IntegrityHasher.calculate_hash({"a": 2})
