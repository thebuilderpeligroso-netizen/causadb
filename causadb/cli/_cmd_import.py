"""CLI handler para `causadb import --format otel --ledger --file` (F.6.3).

Artículo VIII — función, no clase. cmd_import(args) -> tuple delega al
módulo causadb.otel._importer.

Pattern A: retorna (exit_code, output_str) donde output_str es JSON.
El main.py es el único lugar que llama print().
"""
import json
from typing import Tuple

from causadb.otel._importer import OTelImporter


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

    try:
        importer = OTelImporter(args.ledger)
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