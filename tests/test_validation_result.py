import pytest
from causadb._validation_result import ValidationResult

def test_validation_result_is_valid_true():
    assert ValidationResult(is_valid=True).is_valid is True

def test_validation_result_failure_fields():
    vr = ValidationResult(is_valid=False, failure_type="CORRUPTION", position=5, description="xxx")
    assert vr.is_valid is False
    assert vr.failure_type == "CORRUPTION"
    assert vr.position == 5
    assert vr.description == "xxx"

def test_validation_result_bool():
    assert bool(ValidationResult(is_valid=True)) is True
    assert bool(ValidationResult(is_valid=False)) is False
