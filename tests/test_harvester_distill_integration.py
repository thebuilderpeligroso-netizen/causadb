"""Fase 14.1 — Tests de integración: harvest → distill automático.

Artículo III (test-first), Artículo IX (fixture real, no mocks).

Cobertura:
  1. Harvest de hermes fixture + FILE_MODIFIED events → distill automático → skills en ledger.
  2. Ear harvest without detect → no distill (no SKILL_CREATED).
"""

import json
import os
import shutil
from types import MappingProxyType

import pytest

from causadb._harvester import Harvester
from causadb._harvest_source_hermes import HermesHarvestSource
from causadb._skill_registry import load_skills
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
FIXTURE_DB = "hermes_fixture.db"


def _install_fixture(tmp_path):
    """Copia la fixture (state.db real recortado) a un dir temporal."""
    dest = tmp_path / FIXTURE_DB
    shutil.copy(os.path.join(FIXTURE_DIR, FIXTURE_DB), dest)
    return str(dest)


def _make_source(tmp_path, ledger_path=None):
    db_path = _install_fixture(tmp_path)
    return HermesHarvestSource(
        ledger_path=ledger_path or str(tmp_path / "ledger.log"),
        db_path=db_path,
    )


def test_harvest_triggers_distill_creates_skills(tmp_path):
    """Create a Hermes harvest fixture with FILE_MODIFIED events pre-seeded,
    run harvest, verify SKILLS in ledger via load_skills().

    The hermes fixture produces REASONING_STEP, TOOL_CALLED, LLM_INVOKED.
    We pre-seed FILE_MODIFIED events so distill can produce file_tree skill.
    """
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")

    # Pre-seed FILE_MODIFIED events so distill has something to work with
    writer = LedgerWriter(ledger)
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({"path": "causadb/_daemon.py", "action": "modify"}),
    ))
    writer.append(CanonicalEvent(
        event_type=EventType.FILE_MODIFIED, ctx_id="test", source="causadb:test",
        payload=MappingProxyType({"path": "causadb/_ledger_writer.py", "action": "modify"}),
    ))

    source = _make_source(tmp_path, ledger)
    h = Harvester(ledger, config)
    h.register_source(source)

    result = h.harvest_all()

    assert result["hermes"] > 0, f"expected harvest events, got {result}"

    # After harvest, distill should have run and registered skills
    skills = load_skills(ledger)
    assert len(skills) > 0, (
        f"expected skills in ledger after harvest+distill, got {len(skills)}"
    )

    # Verify at least file_tree skill exists (from FILE_MODIFIED events)
    skill_types = [s["skill_type"] for s in skills]
    assert "file_tree" in skill_types, (
        f"expected file_tree skill, got types: {skill_types}"
    )


def test_ear_harvest_no_distill_when_detect_false(tmp_path):
    """Source with detect=False, harvest, verify no SKILL_CREATED."""
    ledger = str(tmp_path / "ledger.log")
    config = str(tmp_path / "cursors.json")

    # Create a hermes source pointing to a non-existent db → detect() = False
    source = HermesHarvestSource(
        ledger_path=ledger,
        db_path=str(tmp_path / "no-existe.db"),
    )
    h = Harvester(ledger, config)
    h.register_source(source)

    result = h.harvest_all()

    # detect=False → no harvest → count = 0
    assert result["hermes"] == 0, f"expected 0 events, got {result}"

    # No skills should be registered
    skills = load_skills(ledger)
    assert len(skills) == 0, (
        f"expected 0 skills when detect=False, got {len(skills)}"
    )