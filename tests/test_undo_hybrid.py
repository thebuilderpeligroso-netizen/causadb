"""Tests del `undo` híbrido — "último estado bueno" cruzando disco y ledger.

Artículo III (test-first), Artículo IX (fixture real, no mocks).

El `undo` debe:
1. Si el archivo EXISTE en disco: calcular el SHA-256 de su contenido ACTUAL
   (modo binario) y elegir como restore_event el ÚLTIMO evento FILE_MODIFIED
   cuyo ``content_hash`` DIERA del hash del disco (detecta el último cambio
   real, incluso si el archivo se tocó a mano después del último evento).
2. Si el archivo NO existe en disco, o ningún evento es comparable
   (content_hash None), o no se encuentra ninguno que difiera: caer al
   comportamiento actual (penúltimo no-deleted).
3. Seguir saltando eventos ``deleted``.

Anti-teatro: los tests escriben el archivo en disco, corren ``cmd_undo`` de
verdad y comparan el contenido restaurado byte a byte. No se mockea la
selección del restore_event.
"""

import hashlib
import os
from types import SimpleNamespace

from causadb._blob_store import BlobStore
from causadb._config import CausaDBConfig
from causadb._event_schema import CanonicalEvent
from causadb._event_types import EventType
from causadb._ledger_writer import LedgerWriter
from causadb.cli._cmd_undo import cmd_undo


def _write_events(tmp_path, versions):
    """Escribe eventos FILE_MODIFIED + blobs para un archivo ``proj.py``.

    ``versions`` es una lista de dicts:
        {"content": str, "content_hash": str|None, "action": str}
    Los blobs se crean en el orden de ``versions`` (el orden de os.walk
    sobre el BlobStore sigue el orden de creación en la mayoría de FS).
    Retorna (ledger, real_file).
    """
    ledger = str(tmp_path / "ledger.log")
    blob_path = str(tmp_path / "blobs")
    workspace = str(tmp_path / "workspace")
    os.makedirs(workspace)

    store = BlobStore(blob_path)
    config = CausaDBConfig(
        ledger_path=ledger,
        blob_store_enabled=True,
        blob_store_path=blob_path,
    )
    writer = LedgerWriter(ledger, config=config)

    real_file = os.path.join(workspace, "proj.py")
    for i, v in enumerate(versions):
        ev_path = "proj.py"
        if v["content"] is not None:
            # Almacenar contenido como blob (como el harvester real).
            store.put({"content": v["content"], "path": ev_path})
        writer.append(CanonicalEvent(
            event_type=EventType.FILE_MODIFIED,
            ctx_id="harvester:filesystem",
            source="harvester:filesystem",
            payload={
                "action": v.get("action", "created" if i == 0 else "modified"),
                "path": ev_path,
                "content_hash": v["content_hash"],
            },
        ))
    return ledger, real_file


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def test_undo_restaura_ultimo_distinto_del_disco(tmp_path):
    """El archivo existe en disco con contenido C (edición manual, distinto
    del último evento). El undo debe restaurar el ÚLTIMO evento cuyo hash
    difiera del disco (v2), no el penúltimo a ciegas (v1).
    """
    v1 = "contenido v1 " + hashlib.sha256(b"v1").hexdigest()[:16]
    v2 = "contenido v2 " + hashlib.sha256(b"v2").hexdigest()[:16]
    manual = "edicion manual " + hashlib.sha256(b"manual").hexdigest()[:16]

    ledger, real_file = _write_events(tmp_path, [
        {"content": v1, "content_hash": _sha(v1), "action": "created"},
        {"content": v2, "content_hash": _sha(v2), "action": "modified"},
    ])

    # El archivo existe en disco con contenido manual (distinto de v2).
    with open(real_file, "wb") as f:
        f.write(manual.encode("utf-8"))

    args = SimpleNamespace(file=real_file, ledger=ledger)
    code, output = cmd_undo(args)
    assert code == 0, f"undo falló: {output}"

    with open(real_file, "rb") as f:
        restored = f.read()
    assert restored == v2.encode("utf-8"), (
        f"El undo híbrido debe restaurar el último evento distinto del disco "
        f"(v2), no el penúltimo a ciegas (v1). Obtenido: {restored!r}"
    )


def test_undo_cae_al_fallback_si_archivo_no_existe_en_disco(tmp_path):
    """El archivo NO existe en disco → el undo cae al comportamiento actual
    (penúltimo no-deleted = v1)."""
    v1 = "contenido v1 " + hashlib.sha256(b"f1").hexdigest()[:16]
    v2 = "contenido v2 " + hashlib.sha256(b"f2").hexdigest()[:16]

    ledger, real_file = _write_events(tmp_path, [
        {"content": v1, "content_hash": _sha(v1), "action": "created"},
        {"content": v2, "content_hash": _sha(v2), "action": "modified"},
    ])

    # El archivo NO existe en disco (no lo creamos).
    assert not os.path.exists(real_file)

    args = SimpleNamespace(file=real_file, ledger=ledger)
    code, output = cmd_undo(args)
    assert code == 0, f"undo falló: {output}"

    with open(real_file, "rb") as f:
        restored = f.read()
    assert restored == v1.encode("utf-8"), (
        f"Sin archivo en disco, el undo debe caer al fallback (v1). "
        f"Obtenido: {restored!r}"
    )


def test_undo_salta_content_hash_none(tmp_path):
    """Eventos con content_hash None intermedios no deben elegirse; el fix
    debe saltarlos y seguir caminando hacia atrás hasta el primer evento
    cuyo hash difiera del disco."""
    v1 = "contenido v1 " + hashlib.sha256(b"n1").hexdigest()[:16]
    v3 = "contenido v3 " + hashlib.sha256(b"n3").hexdigest()[:16]

    ledger, real_file = _write_events(tmp_path, [
        {"content": v1, "content_hash": _sha(v1), "action": "created"},
        {"content": None, "content_hash": None, "action": "modified"},
        {"content": v3, "content_hash": _sha(v3), "action": "modified"},
    ])

    # El archivo existe en disco con el contenido del ÚLTIMO evento (v3).
    with open(real_file, "wb") as f:
        f.write(v3.encode("utf-8"))

    args = SimpleNamespace(file=real_file, ledger=ledger)
    code, output = cmd_undo(args)
    assert code == 0, f"undo falló: {output}"

    with open(real_file, "rb") as f:
        restored = f.read()
    # El evento v2 (content_hash None) no es comparable y debe saltarse;
    # el fix debe llegar hasta v1 (cuyo hash difiere del disco).
    assert restored == v1.encode("utf-8"), (
        f"El undo debe saltar el evento con content_hash None y restaurar v1. "
        f"Obtenido: {restored!r}"
    )