# Shared Workspace — Procedural Skill

Coordinación multi-agente mediante documentos compartidos fijos.

## Qué son

Dos anotadores fijos persistidos en el ledger para coordinación entre agentes:

- **AUDIT_REPORT**: resultado de auditorías (qué se verificó, hallazgos, veredicto)
- **ACTION_PLAN**: plan de acción compartido (fases, responsables, estado)

## Tools

| Tool | Uso |
|------|-----|
| `shared_document_read(name)` | Leer contenido del documento |
| `shared_document_write(name, content)` | Escribir/actualizar contenido |

`name` debe ser exactamente `"AUDIT_REPORT"` o `"ACTION_PLAN"`.

## Cuándo usar

- **Arranque de sesión**: leer `ACTION_PLAN` para retomar contexto compartido
- **Cierre de sesión**: escribir `AUDIT_REPORT` con hallazgos de la sesión
- **Handoff entre agentes**: el Maker escribe su plan, el Checker lo lee
- **Auditoría**: el Checker escribe su veredicto en `AUDIT_REPORT`

## Ejemplo

```python
# Leer plan de acción
result = shared_document_read("ACTION_PLAN")
plan = json.loads(result)

# Escribir reporte de auditoría
report = {
    "verdict": "PASS with observations",
    "files_checked": ["main.py", "test_main.py"],
    "observations": ["Missing edge case in line 42"]
}
shared_document_write("AUDIT_REPORT", json.dumps(report))
```

## Notas

- Los documentos se crean automáticamente en `causadb setup`
- Contenido anterior se sobreescribe (no es historial — usar ledger para historial)
- Formato: JSON válido
