"""Tests J.2 — Deduplication + cursor state.

Cobertura:
  1. ``test_harvester_cursor_stored_in_config`` — tras harvest_all() el
     archivo de config contiene el cursor con el índice avanzado.
  2. ``test_harvester_repeated_run_no_duplicates`` — dos corridas
     consecutivas sin nuevos eventos → la segunda produce 0 eventos
     (dedup vía cursor).
  3. ``test_anti_teatro_cursor_ignored`` — canary anti-teatro: si se
     muta el harvester para ignorar el cursor en la segunda corrida,
     aparecen duplicados y el test FALLA. Verifica que la dedup
     depende del cursor, no de magia escondida.

Convenciones:
  - ``tmp_path`` para config_dir y ledger.
  - Mock source reusado del patrón de ``test_harvester.py``.
  - Sin dependencias externas.
"""

import json
import os
import pytest
from causadb._harvest_source import HarvestSource
from causadb._harvester import Harvester


# ===================================================================
# Mock source — source_type="mock_source", cursor_key="mock_source"
# ===================================================================

class MockSource(HarvestSource):
    """Fuente mock con cursor secuencial ``{"index": N}``.

    Mantiene una lista interna de raw dicts. ``harvest(cursor)`` retorna
    los eventos desde ``cursor["index"]`` hacia adelante. Si el cursor
    apunta al final, retorna ``[]`` (sin duplicados).
    """

    def __init__(self, ledger_path, events=None, detect_result=True):
        super().__init__(ledger_path)
        self._events = list(events or [])
        self._detect_result = detect_result

    def source_type(self):
        return "mock_source"

    def cursor_key(self):
        return "mock_source"

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


# ===================================================================
# Fixtures
# ===================================================================

@pytest.fixture
def ledger_path(tmp_path):
    return str(tmp_path / "ledger.log")


@pytest.fixture
def config_path(tmp_path):
    """Archivo de cursores dentro del config_dir (tmp_path)."""
    return str(tmp_path / "harvest_cursors.json")


def _make_events(n):
    """Genera ``n`` raw dicts distintos (command distinto → event_id
    distinto vía SHA-256)."""
    return [
        {"type": "COMMAND_RUN", "timestamp": f"2024-01-01 10:{i:02d}:00",
         "command": f"cmd_{i}"}
        for i in range(n)
    ]


# ===================================================================
# Tests
# ===================================================================

def test_harvester_cursor_stored_in_config(ledger_path, config_path):
    """Tras harvest_all() con N eventos, el archivo de config contiene
    ``{"mock_source": {"index": N}}``.

    Verifica que el cursor se persiste y avanza correctamente.
    """
    n = 4
    source = MockSource(ledger_path, events=_make_events(n))
    h = Harvester(ledger_path, config_path)
    h.register_source(source)

    result = h.harvest_all()
    assert result["mock_source"] == n, (
        f"Esperaba {n} eventos en 1ª corrida, obtuve {result}"
    )

    # El archivo de config debe existir y contener el cursor avanzado
    assert os.path.exists(config_path), (
        "Archivo de cursores no fue creado tras harvest_all()"
    )
    with open(config_path) as f:
        cursors = json.load(f)

    assert "mock_source" in cursors, (
        f"Cursor 'mock_source' no encontrado en config: {cursors}"
    )
    assert cursors["mock_source"]["index"] == n, (
        f"Cursor debe apuntar a index={n}, tiene {cursors['mock_source']}"
    )


def test_harvester_repeated_run_no_duplicates(ledger_path, config_path):
    """Dos corridas consecutivas sin agregar eventos → la segunda
    produce 0 eventos (dedup vía cursor persistente).

    El ledger debe tener exactamente N entries al final, no 2N.
    """
    n = 3
    source = MockSource(ledger_path, events=_make_events(n))
    h = Harvester(ledger_path, config_path)
    h.register_source(source)

    # --- 1ª corrida: N eventos ---
    r1 = h.harvest_all()
    assert r1["mock_source"] == n, (
        f"1ª corrida: esperaba {n} eventos, obtuve {r1}"
    )

    # --- 2ª corrida: 0 eventos (cursor guardado) ---
    r2 = h.harvest_all()
    assert r2["mock_source"] == 0, (
        f"2ª corrida: esperaba 0 eventos (dedup), obtuve {r2}"
    )

    # Ledger debe tener exactamente N entries (no duplicados)
    with open(ledger_path) as f:
        lines = f.readlines()
    assert len(lines) == n, (
        f"Ledger debe tener {n} entries (sin duplicados), tiene {len(lines)}"
    )

    # Cursor debe seguir apuntando a N (no avanzó porque no hubo eventos)
    with open(config_path) as f:
        cursors = json.load(f)
    assert cursors["mock_source"]["index"] == n, (
        f"Cursor debe seguir en index={n}, tiene {cursors['mock_source']}"
    )


def test_anti_teatro_cursor_ignored(ledger_path, config_path):
    """Anti-teatro: si el harvester IGNORA el cursor en la segunda
    corrida, aparecen duplicados y este test FALLA.

    Este test verifica que la dedup es una propiedad EMERGENTE del
    cursor persistente, no una hardcoded. Si alguien muta el harvester
    para "siempre retornar 0 en la segunda corrida" (teatro), este
    test lo detecta porque al ignorar el cursor aparecen duplicados
    reales en el ledger.

    Procedimiento:
      1. 1ª corrida normal → N eventos.
      2. Mutar ``_load_cursors`` para retornar ``{}`` (ignorar cursor).
      3. 2ª corrida → deben aparecer N duplicados (porque la fuente
         cosecha desde index=0 de nuevo).
      4. Verificar que el ledger tiene 2N entries (N originales + N
         duplicados). Si el harvester está haciendo teatro (retorna 0
         sin mirar el cursor), el ledger tendría N y el assert fallaría.
    """
    n = 3
    source = MockSource(ledger_path, events=_make_events(n))
    h = Harvester(ledger_path, config_path)
    h.register_source(source)

    # --- 1ª corrida normal: N eventos ---
    r1 = h.harvest_all()
    assert r1["mock_source"] == n, (
        f"1ª corrida: esperaba {n} eventos, obtuve {r1}"
    )

    # --- MUTACIÓN: ignorar el cursor en la siguiente corrida ---
    # Parchamos _load_cursors para que retorne {} (cursor vacío).
    # Esto simula un harvester que "olvida" su progreso.
    original_load = h._load_cursors
    h._load_cursors = lambda: {}

    # --- 2ª corrida con cursor ignorado: deben aparecer duplicados ---
    r2 = h.harvest_all()
    assert r2["mock_source"] == n, (
        f"2ª corrida con cursor ignorado: esperaba {n} duplicados, "
        f"obtuve {r2}. Si obtuvo 0, el harvester está haciendo teatro "
        f"(retorna 0 sin mirar el cursor)."
    )

    # Restaurar (buena práctica aunque el test termine aquí)
    h._load_cursors = original_load

    # --- Verificar que el ledger tiene 2N entries (N + N duplicados) ---
    with open(ledger_path) as f:
        lines = f.readlines()
    assert len(lines) == 2 * n, (
        f"Ledger debe tener {2*n} entries ({n} originales + {n} "
        f"duplicados por cursor ignorado), tiene {len(lines)}. "
        f"Si tiene {n}, el harvester está suprimiendo duplicados por "
        f"otra vía (teatro), no por cursor."
    )

    # --- Verificar que los event_ids de la 2ª tanda son idénticos a
    # los de la 1ª (mismo raw → mismo SHA-256 → duplicado real) ---
    event_ids_first = set()
    event_ids_second = set()
    with open(ledger_path) as f:
        for i, line in enumerate(f):
            entry = json.loads(line)
            eid = entry["event"]["event_id"]
            if i < n:
                event_ids_first.add(eid)
            else:
                event_ids_second.add(eid)

    assert event_ids_first == event_ids_second, (
        "Los event_ids de la 2ª corrida deben ser idénticos a los de "
        "la 1ª (mismo raw → mismo SHA-256). Si son distintos, algo "
        "está mutando el contenido y el test no valida dedup real."
    )
