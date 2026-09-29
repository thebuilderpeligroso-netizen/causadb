"""CLI handler para `causadb import --format otel --ledger --file` (F.6.3).

Artículo VIII — función, no clase. cmd_import(args) -> tuple delega al
módulo causadb.otel._importer.

Pattern A: retorna (exit_code, output_str) donde output_str es JSON.
El main.py es el único lugar que llama print().
"""
import json
from typing import Tuple

from causadb._otel_require import try_import_otel


def cmd_import(args) -> Tuple[int, str]:
    """Handler for `causadb import --format otel --ledger --file`.

    Args:
        args: Namespace argparse with format, ledger, file.

    Returns:
        (exit_code, json_str) — exit 0 if success, 1 if error.
    """
    fmt = getattr(args, "format", "otel")
    if fmt != "otel":
        return (
            1,
            json.dumps({
                "error": f"format not supported: {fmt}",
                "supported": ["otel"],
            }),
        )

    # Lazy OTel: el import va DENTRO de la función via helper compartido
    # (el top-level `from causadb.otel...` crasheaba first-install sin el
    # extra `dev` por la cadena __init__ → _mapper → opentelemetry.sdk).
    importer_cls, _otel_error = try_import_otel(
        "causadb.otel._importer", "OTelImporter"
    )
    if _otel_error is not None:
        return _otel_error

    try:
        importer = importer_cls(args.ledger)
        result = importer.import_file(args.file)
        return (0, json.dumps(result, sort_keys=True))
    except Exception as e:
        return (
            1,
            json.dumps({
                "error": str(e),
                "error_type": type(e).__name__,
            }),
        )