"""Fase 4 C-05 — Symlink escape + no-regulares (RED).

- Symlink externo (file y dir) saltado + poda de dirnames.
- Symlink interno cosechado.
- No-regulares (fifo) saltados.
- Forward-only: sin purga histórica (no se exige borrar nada previo).
"""
import os

from causadb._harvest_source_filesystem import FilesystemSource


def test_external_symlinks_skipped_internal_harvested(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top-secret")

    # Archivo real interno + link interno hacia él.
    (project / "real.txt").write_text("hello")
    os.symlink(str(project / "real.txt"), str(project / "inner_link.txt"))

    # Link externo (file) → fuera del root.
    os.symlink(str(outside / "secret.txt"), str(project / "evil_link.txt"))

    # Dir externo con contenido + symlink a ese dir.
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "evil_inside.txt").write_text("evil")
    os.symlink(str(outside_dir), str(project / "evil_dir"))

    # FIFO (no-regular) debe saltarse.
    os.mkfifo(str(project / "pipe.fifo"))

    source = FilesystemSource(
        ledger_path="/fake/ledger.log",
        project_root=str(project),
    )
    events = list(source.harvest())
    paths = {e["path"] for e in events}

    assert "real.txt" in paths
    assert "inner_link.txt" in paths, "link interno debe cosecharse"
    assert "evil_link.txt" not in paths, "link externo (file) debe saltarse"
    assert not any(p.startswith("evil_dir") for p in paths), (
        f"dir externo podado, got {sorted(paths)}"
    )
    assert "evil_inside.txt" not in paths
    assert "pipe.fifo" not in paths, "fifo (no-regular) debe saltarse"
