# State Reconstruction — Procedural Skill

Patrones de reconstrucción dirigida de estado desde el ledger CausaDB.

## Disparadores

| Pregunta | Patrón |
|----------|--------|
| ¿Qué hizo el agente X en la última sesión? | P1 |
| ¿Qué archivo tocó, cuándo y con qué contenido? | P2 |
| Restaurar un archivo a su estado previo | P3 |
| ¿Por qué existe esta línea? | P4 |
| ¿El agente metió mano donde no debía? | P5 |
| ¿El DONE que reportó es verdad? | P6 |
| ¿Qué hicimos sobre X y por qué? | P7 |
| Auditar a un Maker por evidencia | P8 |
| Reconstruir la prosa de un BIT | P9 |

## Patrones

### P1 — Última sesión de un agente
1. `causadb_revive` → últimas decisiones + particiones preloaded
2. `causadb_ocb_status` → partición por `session_ids`/`sources`
3. `causadb_ocb_load_partition` → `TOOL_CALLED`/`REASONING_STEP` en orden

### P2 — Historial de un archivo
- `causadb_query(event_type="FILE_MODIFIED", text="<path>")`
- Devuelve snapshots con `action`, `timestamp`, `content_hash`, `content`

### P3 — Restaurar archivo
1. `causadb_query(event_type="FILE_MODIFIED", text="<path>")`
2. Escribir `payload.content` en disco
3. Verificar: `sha256sum` == `content_hash`

### P4 — Atribución causal
- `causadb_why(file, line)` → evento que introdujo la línea
- `causadb_trace(file, line)` → cone causal upstream completo
- `causadb_impact(event_id)` → blast radius downstream

### P5 — Sandbox violations
- `causadb_sandbox` → violaciones + mutaciones totales
- Complementar con `causadb_score` (churn/waste/survival)

### P6 — Verificar reporte DONE
1. `causadb_query(event_type="FILE_MODIFIED", text="<archivo>")`
2. Cruzar con `pytest` real + `causadb_validate`/`causadb_sentinel`
3. Verificar `COMMAND_RUN` para claims numéricos

### P7 — BITs sobre un tema
1. `grep "X" CAUSADB_CHRONICLE.md` → localizar BITs
2. `causadb chronicle list` → índice con conteo de eventos
3. `causadb chronicle events --bit <BIT>` → event_ids enlazados
4. `causadb chronicle reconstruct --bit <BIT>` → estado al momento del BIT

### P8 — Auditoría de Maker
1. `causadb_ocb_status` → localizar partición del Maker
2. `causadb_ocb_load_partition` → secuencia temporal completa
3. Verificar: orden RED→GREEN, alcance, claims vs evidencia
4. `causadb_why`/`causadb_trace` para líneas sospechosas

### P9 — Reconstruir prosa de decisión
1. `causadb_query(event_type="GOVERNANCE_DECISION", text="<BIT>")` → reasoning
2. `causadb_revive`/`causadb_ocb_status` → partición en ventana temporal
3. Extraer `session_id` de `REASONING_STEP`/`TOOL_CALLED`
4. `causadb recover <session_id>` → storyboard completo desde fuente cruda
