"""Tests J.1 — Harvester / Sedimenter core.

Cobertura:
  1. ``test_harvester_reads_shell_history`` — lectura básica → eventos
  2. ``test_harvester_skips_already_logged`` — cursor evita duplicados
  3. ``test_harvester_handles_missing_source`` — detect()=False no crashea
  4. ``test_harvester_normalizes_timestamp`` — SQL → ISO 8601
  5. ``test_anti_teatro_harvester_fixed_output`` — canary anti-hardcodeo
"""

import json
import os
import pytest
from causadb._harvest_source import HarvestSource
from causadb._harvester import Harvester, normalize_timestamp


# ===================================================================
# Mock sources
# ===================================================================

class MockHarvestSource(HarvestSource):
    """Fuente mock que simula una shell history o similar.

    Mantiene una lista interna de raw dicts y un flag detect().
    El cursor sigue un índice secuencial (``{"index": N}``).
    """

    def __init__(self, ledger_path, events=None, detect_result=True):
        super().__init__(ledger_path)
        self._events = list(events or [])
        self._detect_result = detect_result

    def source_type(self):
        return "mock"

    def cursor_key(self):
        return "mock_cursor"

    def detect(self):
        return self._detect_result

    def harvest(self, cursor=None):
        if not self._events:
            return []
        if cursor is None:
            return list(self._events)
        idx = cursor.get("index", 0)
        if idx >= len(self._events):
            return []
        return list(self._events[idx:])

    def add_events(self, new_events):
        """Agrega eventos hacia atrás (simula nueva actividad)."""
        self._events.extend(new_events)


class MockAgentSource(MockHarvestSource):
    """Fuente mock de agente: ``source_type`` está en ``_AGENT_SOURCES``
    del harvester (opencode) → dispara el feed al OCB (Fase 0)."""

    def source_type(self):
        return "opencode"

    def cursor_key(self):
        return "opencode_cursor"


# ===================================================================
# Fixtures
# ===================================================================

@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")


@pytest.fixture
def config_path(tmp_path):
    return str(tmp_path / ".harvester_cursors.json")


# ===================================================================
# Tests
# ===================================================================

def test_harvester_reads_shell_history(ledger_path, config_path):
    """Crea mock de .bash_history, el harvester lo lee y produce
    eventos COMMAND_RUN."""
    events = [
        {"type": "COMMAND_RUN", "timestamp": "2024-01-01 10:00:00",
         "command": "ls -la"},
        {"type": "COMMAND_RUN", "timestamp": "2024-01-01 10:01:00",
         "command": "cd /tmp"},
    ]
    source = MockHarvestSource(ledger_path, events=events)
    h = Harvester(ledger_path, config_path)
    h.register_source(source)

    result = h.harvest_all()
    assert result["mock"] == 2, f"Esperaba 2 eventos, obtuve {result}"

    # Verificar ledger
    with open(ledger_path) as f:
        lines = f.readlines()
    assert len(lines) == 2, f"Ledger debe tener 2 entries, tiene {len(lines)}"

    e1 = json.loads(lines[0])["event"]
    assert e1["event_type"] == "COMMAND_RUN"
    assert e1["payload"]["command"] == "ls -la"
    assert e1["source"] == "harvester:mock"
    assert e1["source_type"] == "agent"
    assert e1["timestamp"].endswith("Z")  # normalizado

    e2 = json.loads(lines[1])["event"]
    assert e2["event_type"] == "COMMAND_RUN"
    assert e2["payload"]["command"] == "cd /tmp"
    assert e2["event_id"] != e1["event_id"]  # eventos distintos


def test_harvester_skips_already_logged(ledger_path, config_path):
    """Primera pasada: 5 eventos. Segunda (cursor guardado): 0.
    Después agregar 2 líneas → tercera pasada: 2 eventos."""
    initial_events = [
        {"type": "COMMAND_RUN", "timestamp": f"2024-01-01 10:{i:02d}:00",
         "command": f"cmd{i}"}
        for i in range(5)
    ]
    source = MockHarvestSource(ledger_path, events=list(initial_events))
    h = Harvester(ledger_path, config_path)
    h.register_source(source)

    # --- 1ª pasada: 5 eventos ---
    r1 = h.harvest_all()
    assert r1["mock"] == 5, f"1ª pasada: esperaba 5, obtuve {r1}"

    # --- 2ª pasada: 0 (cursor guardado) ---
    r2 = h.harvest_all()
    assert r2["mock"] == 0, f"2ª pasada: esperaba 0, obtuve {r2}"

    # --- Agregar 2 eventos nuevos ---
    source.add_events([
        {"type": "COMMAND_RUN", "timestamp": "2024-01-01 11:00:00",
         "command": "new_cmd1"},
        {"type": "COMMAND_RUN", "timestamp": "2024-01-01 11:01:00",
         "command": "new_cmd2"},
    ])

    # --- 3ª pasada: 2 eventos nuevos ---
    r3 = h.harvest_all()
    assert r3["mock"] == 2, f"3ª pasada: esperaba 2, obtuve {r3}"

    # Total en ledger = 7
    with open(ledger_path) as f:
        lines = f.readlines()
    assert len(lines) == 7, (
        f"Ledger debe tener 7 entries total, tiene {len(lines)}"
    )

    # Verificar que el archivo de cursores existe y tiene el índice correcto
    assert os.path.exists(config_path), "Cursor file no fue creado"
    with open(config_path) as f:
        cursors = json.load(f)
    assert cursors["mock_cursor"]["index"] == 7, (
        f"Cursor debe apuntar a index=7, tiene {cursors}"
    )


def test_harvester_handles_missing_source(ledger_path, config_path):
    """Fuente con detect()=False → no crashea, retorna 0."""
    source = MockHarvestSource(ledger_path, detect_result=False)
    h = Harvester(ledger_path, config_path)
    h.register_source(source)

    result = h.harvest_all()
    assert result["mock"] == 0, (
        f"Fuente no detectada debe retornar 0, obtuve {result}"
    )

    # Ledger debe estar vacío (solo genesis si existía)
    assert not os.path.exists(ledger_path) or os.path.getsize(ledger_path) == 0, (
        "No deben haberse escrito eventos al ledger"
    )

    # Cursor NO debe haberse guardado para esta fuente
    assert not os.path.exists(config_path) or os.path.getsize(config_path) == 0 or (
        json.load(open(config_path)) == {}
    ), "No deben persistirse cursores para fuentes no detectadas"


def test_harvester_normalizes_timestamp(ledger_path, config_path):
    """Fuente devuelve timestamp '2024-01-01 12:00:00' (SQL)
    → se normaliza a ISO 8601."""
    events = [
        {"type": "COMMAND_RUN", "timestamp": "2024-01-01 12:00:00",
         "command": "ls"},
    ]
    source = MockHarvestSource(ledger_path, events=events)
    h = Harvester(ledger_path, config_path)
    h.register_source(source)
    h.harvest_all()

    with open(ledger_path) as f:
        entry = json.loads(f.readline())

    ts = entry["event"]["timestamp"]
    assert ts.endswith("Z"), f"Timestamp debe terminar con Z, got: {ts}"
    assert ts == "2024-01-01T12:00:00Z", (
        f"Esperaba ISO 8601 normalizado, got: {ts}"
    )

    # También probar normalize_timestamp directamente
    assert normalize_timestamp("2024-06-15 08:30:00") == "2024-06-15T08:30:00Z"
    assert normalize_timestamp(1700000000) == "2023-11-14T22:13:20Z"
    assert normalize_timestamp("2024-01-01T12:00:00Z") == "2024-01-01T12:00:00Z"


def test_anti_teatro_harvester_fixed_output(ledger_path, config_path):
    """Anti-teatro: canary que verifica que eventos cosechados son
    VARIADOS (no hardcodeados).

    Si un implementador muta el harvester o la fuente para hardcodear
    su output (siempre ``[{"type": "COMMAND_RUN"}]`` fijo), este test
    falla porque los event_ids serían idénticos (mismo raw → mismo
    SHA-256) o los event_types no variarían.
    """
    events = [
        {"type": "COMMAND_RUN",    "timestamp": "2024-01-01 10:00:00",
         "command": "ls"},
        {"type": "FILE_MODIFIED",  "timestamp": "2024-01-01 10:01:00",
         "path": "/tmp/a"},
        {"type": "COMMAND_RUN",    "timestamp": "2024-01-01 10:02:00",
         "command": "pwd"},
    ]
    source = MockHarvestSource(ledger_path, events=list(events))
    h = Harvester(ledger_path, config_path)
    h.register_source(source)

    result = h.harvest_all()
    assert result["mock"] == 3

    with open(ledger_path) as f:
        lines = f.readlines()
    assert len(lines) == 3

    event_ids = set()
    event_types = set()
    for line in lines:
        entry = json.loads(line)
        event_ids.add(entry["event"]["event_id"])
        event_types.add(entry["event"]["event_type"])

    # Si alguien hardcodea output, todos los eventos tendrían el
    # mismo raw → mismo SHA-256 → mismo event_id → len==1.
    assert len(event_ids) == 3, (
        f"Anti-teatro: esperaba 3 event_ids únicos, obtuve "
        f"{len(event_ids)}. Source puede estar hardcodeando output."
    )
    assert len(event_types) == 2, (
        f"Anti-teatro: esperaba 2 tipos de evento (COMMAND_RUN, "
        f"FILE_MODIFIED), obtuve {len(event_types)}."
    )


def test_harvester_isolates_source_failure(ledger_path, config_path):
    """Auditoría I.2 — aislamiento por fuente (BIT-CHR.41; docs/design_index.md).

    Anti-teatro: una fuente cuya ``harvest()`` LANZA una excepción (no solo
    un write fallido) NO debe abortar las demás fuentes NI perder el
    guardado de cursores de las que SÍ escribieron (evita re-harvest en
    cascada).
    """
    good_events = [
        {"type": "COMMAND_RUN", "timestamp": "2024-01-01 10:00:00",
         "command": "ls"},
    ]
    good = MockHarvestSource(ledger_path, events=list(good_events))

    class ExplodingSource(MockHarvestSource):
        def source_type(self):
            return "exploding"

        def harvest(self, cursor=None):
            raise RuntimeError("sqlite corrupto")

    exploding = ExplodingSource(ledger_path)

    h = Harvester(ledger_path, config_path)
    h.register_source(exploding)
    h.register_source(good)

    # La excepción de harvest() en 'exploding' se aísla: 'good' sigue
    # escribiendo y su cursor se guarda.
    result = h.harvest_all()
    assert "exploding" in result and result["exploding"] == 0
    assert result["mock"] == 1

    with open(ledger_path) as f:
        lines = f.readlines()
    assert len(lines) == 1, "Solo 'good' debe escribir su evento"

    # Cursor de 'good' guardado → segunda corrida no re-cosecha
    r2 = h.harvest_all()
    assert r2["mock"] == 0, "Cursor debe haberse guardado pese al fallo ajeno"


# ===================================================================
# Fase 0 — El harvest alimenta el OCB (deuda #13)
# ===================================================================


def test_harvest_flushes_active_to_partition(ledger_path, config_path):
    # FIX.OCB-FLUSH: el harvest de una fuente de agente (is_agent)
    # archiva su ACTIVE como PARTITION al final de la corrida (el harvester
    # nunca llama close_session, FIX.2).
    # Sin el flush, _init_workspace de la siguiente instanciación
    # orphanea el ACTIVE residual y revive/resume quedan congelados en el
    # ultimo rebuild manual.
    # Run 1 (3 eventos esenciales): NO hay OCB_ACTIVE.log con contenido,
    # existe >= 1 OCB_PARTITION_*, y las particiones archivan exactamente
    # los 3 eventos (un evento -> un append, sin duplicacion).
    # Run 2 (2 eventos esenciales): el run 1 NO se orphanea -- 0 OCB_ORPHAN_*
    # y >= 2 OCB_PARTITION_*.
    events = [
        {"type": "FILE_MODIFIED", "timestamp": "2024-01-01 10:00:00",
         "__harvest_session_id": "sess-1"},
        {"type": "GOVERNANCE_DECISION", "timestamp": "2024-01-01 10:01:00",
         "reasoning": "test", "impact": "minor", "decision_type": "approve", "origin": "test"},
        {"type": "PROJECT_SNAPSHOT", "timestamp": "2024-01-01 10:02:00",
         "total_events": 10, "total_tests": 5, "fases_completadas": 3, "bloqueantes_resueltos": 2, "notas": "test"},
    ]
    source = MockAgentSource(ledger_path, events=events)
    h = Harvester(ledger_path, config_path)
    h.register_source(source)

    result = h.harvest_all()
    assert result["opencode"] == 3, f"esperaba 3 eventos, got {result}"

    ocb_dir = os.path.join(os.path.dirname(ledger_path), "ocb")
    active = os.path.join(ocb_dir, "OCB_ACTIVE.log")
    assert not os.path.exists(active) or os.path.getsize(active) == 0, (
        "Tras el harvest, el ACTIVE debe estar archivado (flush a PARTITION)"
    )

    partitions = sorted(
        [f for f in os.listdir(ocb_dir)
         if f.startswith("OCB_PARTITION_") and f.endswith(".log")]
    )
    assert len(partitions) >= 1, (
        f"El flush debe crear al menos 1 OCB_PARTITION_*, "
        f"got {os.listdir(ocb_dir)}"
    )

    total_lines = 0
    for pf in partitions:
        with open(os.path.join(ocb_dir, pf)) as f:
            total_lines += sum(1 for l in f if l.strip())
    assert total_lines == 3, (
        f"Las particiones deben archivar exactamente los 3 eventos "
        f"esenciales, got {total_lines} lines"
    )

    # --- Run 2: 2 eventos esenciales nuevos; el run 1 NO debe orphanearse ---
    source.add_events([
        {"type": "FILE_MODIFIED", "timestamp": "2024-01-01 11:00:00",
         "__harvest_session_id": "sess-2"},
        {"type": "GOVERNANCE_DECISION", "timestamp": "2024-01-01 11:01:00",
         "reasoning": "test2", "impact": "minor", "decision_type": "reject", "origin": "test2"},
    ])
    r2 = h.harvest_all()
    assert r2["opencode"] == 2, f"esperaba 2 eventos nuevos, got {r2}"

    orphans = [f for f in os.listdir(ocb_dir) if f.startswith("OCB_ORPHAN_")]
    assert len(orphans) == 0, (
        f"Sin flush, _init_workspace orphanea el ACTIVE residual de la "
        f"corrida anterior; got {orphans}"
    )
    partitions_after = [
        f for f in os.listdir(ocb_dir)
        if f.startswith("OCB_PARTITION_") and f.endswith(".log")
    ]
    assert len(partitions_after) >= 2, (
        f"Tras 2 corridas debe haber >= 2 particiones (1 por corrida), "
        f"got {partitions_after}"
    )

    total_lines_after = 0
    for pf in partitions_after:
        with open(os.path.join(ocb_dir, pf)) as f:
            total_lines_after += sum(1 for l in f if l.strip())
    assert total_lines_after == 5, (
        f"Total 5 lines en 2 particiones (3+2 esenciales), got {total_lines_after}"
    )