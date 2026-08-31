"""Phase 7.4 — Chronicle Migrator.

Parses CAUSADB_CHRONICLE.md into structured dicts suitable for
CHRONICLE_ENTRY events in the CausaDB ledger.

Format expected (old):
    ## BIT-XX — Title

    **Fecha:** YYYY-MM-DD
    **Maker:** nombre
    **Checker:** nombre
    **Archivos tocados:** file1, file2
    **Resumen:** texto...

Format accepted (new, template curado de `causadb chronicle append-md`):
    ## BIT-XX — Title

    **Fecha:** YYYY-MM-DD
    **Autor:** Maker (+ Checker)
    **Naturaleza:** FIX CERRADO — ...

    {body markdown}
    **Referencias:** event_id: <uuid>
    ---

`**Autor:**` se mapea a maker (y checker = autor, o split en "+" si ambos
roles están en la misma línea). `**Naturaleza:**` alimenta `summary` cuando
no hay `**Resumen:**` (compat con el formato viejo).
"""

import os
import re
from typing import List, Dict, Optional


def parse_chronicle_md(chronicle_path: str) -> List[Dict]:
    """Parse a CAUSADB_CHRONICLE.md file into a list of entry dicts.

    Each dict contains: bit_id, title, date, maker, checker,
    files_touched (list), summary.

    Returns an empty list if the file doesn't exist or has no BIT entries.
    """
    if not chronicle_path or not os.path.exists(chronicle_path):
        return []

    try:
        with open(chronicle_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (IOError, OSError):
        return []

    # Split on ## BIT- markers (but not ### sub-headings)
    # Pattern: ## BIT-XXX — Title (at start of line)
    entries = []
    # Use regex to find all BIT sections
    # Match: ## BIT-<id> — <title> followed by content until next ## BIT- or end
    pattern = re.compile(
        r'^##\s+(BIT-\S+)\s*[—–-]\s*(.+?)$\s*\n(.*?)(?=^##\s+BIT-|\Z)',
        re.MULTILINE | re.DOTALL
    )

    for match in pattern.finditer(content):
        bit_id = match.group(1).strip()
        title = match.group(2).strip()
        body = match.group(3).strip()

        entry = _parse_entry_body(bit_id, title, body)
        if entry:
            entries.append(entry)

    return entries


def _parse_entry_body(bit_id: str, title: str, body: str) -> Optional[dict]:
    """Parse the body of a single BIT entry into a structured dict."""
    entry = {
        "bit_id": bit_id,
        "title": title,
        "date": "",
        "maker": "",
        "checker": "",
        "files_touched": [],
        "summary": "",
    }

    # Extract **Fecha:** YYYY-MM-DD
    date_match = re.search(r'\*\*Fecha:\*\*\s*(.+?)\s*$', body, re.MULTILINE)
    if date_match:
        entry["date"] = date_match.group(1).strip()

    # Extract **Maker:** name (old format)
    maker_match = re.search(r'\*\*Maker:\*\*\s*(.+?)\s*$', body, re.MULTILINE)
    if maker_match:
        entry["maker"] = maker_match.group(1).strip()

    # Extract **Checker:** name (old format)
    checker_match = re.search(r'\*\*Checker:\*\*\s*(.+?)\s*$', body, re.MULTILINE)
    if checker_match:
        entry["checker"] = checker_match.group(1).strip()

    # New format: **Autor:** → maker/checker (split en "+" si ambos roles).
    # Solo aplica si falta maker o checker (los campos del formato viejo
    # son más específicos y ganan).
    if not entry["maker"] or not entry["checker"]:
        autor_match = re.search(r'\*\*Autor:\*\*\s*(.+?)\s*$', body, re.MULTILINE)
        if autor_match:
            autor = autor_match.group(1).strip()
            parts = [p.strip() for p in autor.split("+") if p.strip()]
            if len(parts) >= 2:
                entry["maker"] = entry["maker"] or parts[0]
                entry["checker"] = entry["checker"] or parts[1]
            else:
                entry["maker"] = entry["maker"] or autor
                entry["checker"] = entry["checker"] or autor

    # Extract **Archivos tocados:** file1, file2, ...
    files_match = re.search(r'\*\*Archivos tocados:\*\*\s*(.+?)\s*$', body, re.MULTILINE)
    if files_match:
        files_str = files_match.group(1).strip()
        # Split by comma and strip whitespace
        entry["files_touched"] = [
            f.strip() for f in files_str.split(",") if f.strip()
        ]

    # Extract **Resumen:** text (can span multiple lines until next ** or end)
    summary_match = re.search(
        r'\*\*Resumen:\*\*\s*(.+?)(?=\n\*\*|\Z)',
        body,
        re.MULTILINE | re.DOTALL
    )
    if summary_match:
        entry["summary"] = summary_match.group(1).strip()
    else:
        # New format: **Naturaleza:** (single line) → summary
        nature_match = re.search(r'\*\*Naturaleza:\*\*\s*(.+?)\s*$', body, re.MULTILINE)
        if nature_match:
            entry["summary"] = nature_match.group(1).strip()

    return entry