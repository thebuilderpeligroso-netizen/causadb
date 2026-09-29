"""Helper compartido para imports OTel opcionales (first-install adoption).

Doctrina ``pyproject:40`` — el núcleo queda SIN OTel como dep dura;
OTel vive en el extra ``dev``
(``pyproject.toml`` ``[project.optional-dependencies]``).

El crash OTel viene por la cadena ``causadb/otel/__init__.py:15`` →
``_mapper.py:37-39`` (importa ``opentelemetry.sdk`` al top);
``_importer.py`` es stdlib-limpio. Pero CUALQUIER import bajo
``causadb.otel`` (incluso del ``_importer`` limpio) ejecuta primero el
``__init__`` del parent package → la cadena entera. Por eso los
``_cmd_*`` NO importan OTel al top: llaman a este helper DENTRO de la
función, y el ``try/except`` cubre TODA la cadena
(``importlib.import_module`` corre el ``__init__`` del parent).

Pattern A: ante falta de OTel devuelve ``(exit_code=1, json_str)``
con keys ``error`` + ``hint`` (install del extra real ``causadb[dev]``,
verificado en ``pyproject.toml``, NO inventar ``causadb[otel]``).
"""

import importlib
import json
from typing import Any, Optional, Tuple

# Extra real verificado en pyproject.toml [project.optional-dependencies].
OTEL_EXTRA = "dev"
OTEL_HINT = "pip install 'causadb[dev]' to enable OTel import/export"


def try_import_otel(
    module_name: str, attr: str
) -> Tuple[Any, Optional[Tuple[int, str]]]:
    """Intenta importar ``attr`` desde ``module_name`` bajo ``causadb.otel``.

    Args:
        module_name: módulo dotted (ej: ``"causadb.otel._importer"``).
        attr: atributo a extraer (ej: ``"OTelImporter"``).

    Returns:
        ``(obj, None)`` si el import funcionó; ``(None, (1, json_str))``
        Pattern A con ``error`` + ``hint`` si falta OTel.
    """
    try:
        module = importlib.import_module(module_name)
        return getattr(module, attr), None
    except ImportError as e:
        # ImportError cubre ModuleNotFoundError (paquete ausente en un
        # first-install sin el extra `dev`) y el ImportError de un
        # sys.modules bloqueado; ambos significan "sin OTel".
        # NO se captura AttributeError: un attr inexistente con OTel
        # instalado es un bug real (fail-closed en el caller).
        return None, (
            1,
            json.dumps({
                "error": f"OTel support requires the 'dev' extra: {e}",
                "hint": OTEL_HINT,
            }),
        )
