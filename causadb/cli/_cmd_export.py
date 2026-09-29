"""CLI handler para `causadb export --format otel` (F.6.2).

Artículo VIII — función, no clase. `cmd_export(args) -> tuple` delega al
módulo `causadb.otel._exporter`.

Pattern A: retorna `(exit_code, output_str)` donde `output_str` es JSON.
El `main.py` es el único lugar que llama `print()`.
"""

import json

from causadb._otel_require import try_import_otel


def cmd_export(args) -> tuple:
    """Handler para `causadb export --format otel --ledger --endpoint`.

    Args:
        args: Namespace argparse con `format`, `ledger`, `endpoint`,
            y opcionalmente `headers`.

    Returns:
        (exit_code, json_str) — exit 0 si success, 1 si error.
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
    export_ledger, _otel_error = try_import_otel(
        "causadb.otel._exporter", "export_ledger"
    )
    if _otel_error is not None:
        return _otel_error

    try:
        result = export_ledger(
            args.ledger,
            args.endpoint,
            getattr(args, "headers", None),
        )
        return (0, json.dumps(result, sort_keys=True))
    except Exception as e:
        return (
            1,
            json.dumps({
                "error": str(e),
                "error_type": type(e).__name__,
            }),
        )
