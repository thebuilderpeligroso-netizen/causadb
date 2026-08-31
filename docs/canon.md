# CausaDB — Canon para agentes IA

Si sos un agente de IA que entra a un proyecto con CausaDB, este documento te dice lo mínimo indispensable para que la memoria del proyecto te sirva. Es agnóstico a la herramienta que te hospeda (cualquier agente compatible con MCP: Claude, Cursor, gemini-cli, opencode, otros) — no prescribe nada sobre tu forma de trabajar, solo cómo interactuar con CausaDB para que cumpla lo que promete.

---

## 1. Qué es CausaDB

CausaDB es un **middleware de memoria para agentes IA** con hash-chain criptográfica. Registra side effects de los agentes (archivos que modifican, comandos que corren, git commits que hacen, decisiones que toman) en un ledger inmutable y los recupera a pedido. No compite con LangSmith, LangChain, CrewAI o backend de observabilidad — esos son para monitoreo realtime. CausaDB es para reconstrucción determinista del pasado.

El ledger `ledger.log` es una secuencia de eventos ordenados, cada uno firmado con un hash que depende del evento anterior (modificar o reordenar eventos quiebra la cadena). Replay es 100% determinista: el estado del proyecto es función única de los eventos en el ledger.

---

## 2. Las 19 tools MCP que tenés a mano

Cualquier agente compatible con MCP se conecta con `causadb config mcp --auto`. Estas son las tools, agrupadas por cuándo usarlas:

### Escritura (registrar cosas nuevas)

| Tool | Cuándo usarla |
|------|--------------|
| `causadb_log` | Apendar un evento al ledger (FILE_MODIFIED, COMMAND_RUN, OBSERVATION, etc.). Pasá el evento como JSON + `ledger_path`. |
| `causadb_log_decision` | Asentar una decisión de governance (cambio arquitectural, fix no trivial, decisión estratégica). Esta es la tool clave para que el próximo agente pueda entender POR QUÉ se hicieron X cosas. Pasá `reasoning` + `decision_type` (strategic/architectural/tactical/revert) + `impact` (critical/high/medium/low). |
| `causadb_chronicle_append` | Sedimentar un BIT-entry al CAUSADB_CHRONICLE.md con template curado (idempotente, FAIL-CLOSED). Reemplaza el edit manual del agente sobre el Chronicle (Layer 3, humana). Params: `ledger_path` + `bit`/`title`/`date`/`author`/`nature`/`summary`/`files`/`body`/`event_id`. El `bit_id` compartido + el `event_id` en `**Referencias:**` alinean ledger ↔ .md. |

### Reconstrucción (recuperar el pasado)

| Tool | Cuándo usarla |
|------|--------------|
| `causadb_revive` | **Arranque de sesión.** Devuelve un contexto markdown con Chronicle + últimas decisiones + particiones OCB preloaded. Barato. Usá siempre ésta primero antes de `replay`. |
| `causadb_ocb_status` | Estado del OCB (cache L1 de corto plazo). Cuenta de particiones, sesiones activas, fuentes. Para ver qué tan fresca es la memoria de corto plazo. |
| `causadb_ocb_load_partition` | Carga una partición OCB específica bajo demanda (resuelve blobs si quedaste). Para detalle granular después de ver `ocb_status`. |
| `causadb_query` | Filtrado puntual por `event_type`, `ctx_id`, `text`, rango temporal, `source`. Para buscar "todos los FILE_MODIFIED de ayer en este tool". Medio costo. |
| `causadb_replay` | Reconstrucción completa del estado del proyecto desde el ledger en orden. Caro. Úsalo solo si revive/OCB/query no te dieron suficiente contexto. |
| `causadb_recover` | **Storyboard completo de una sesión de agente desde la fuente cruda** (short name MCP: `recover`). Dado un `session_id` (o `search=<keyword>` sobre storyboards persistidos) reconstruye el detalle íntegro de la conversación — prompts, respuestas, reasoning, tool_calls — que el harvest lossy descarta. Paridad CLI `causadb recover`. Sin `tool` explícito la auto-detección recorre las 9 fuentes y puede dar `AmbiguousSessionError`. |

### Atribución causal (por qué pasó una cosa)

| Tool | Cuándo usarla |
|------|--------------|
| `causadb_why` | Dado un archivo + línea, te dice qué evento introdujo esa línea (causal blame). Para entender "quién escribió esto y por qué". |
| `causadb_trace` | Dado un archivo + línea, te da el causal cone completo upstream (todos los eventos que causalmente llevaron a esa línea). |
| `causadb_impact` | Dado un `event_id`, te da el causal cone downstream (qué eventos fueron afectados por ese evento). Para "blast radius" de un bug. |

### Integridad y score

| Tool | Cuándo usarla |
|------|--------------|
| `causadb_validate` | Verifica la hash-chain del ledger. Si algo se corrompió, te dice posición + descripción. Úsalo si sospechás que el ledger fue tocado. |
| `causadb_sentinel` | Corre reglas de integridad (hash, replay, causal) contra el ledger. Para auditoría periódica. |
| `causadb_score` | Score 0-100 de eficiencia: `churn` (líneas escritas y borradas), `waste` (costo de LLM en código revertido), `survival` (razón del código final). Para diagnosticar deuda operativa. |
| `causadb_sandbox` | Reconstruye estado + devuelve resumen de mutaciones detectadas. Para ver si el agente metió mano en archivos que no debía. |

### Memoria de patrones

| Tool | Cuándo usarla |
|------|--------------|
| `causadb_skill_list` | Lista "skills" (patrones persistidos en el ledger de sesiones pasadas — file trees, convenciones, decisiones). El agente puede usarlas para comprimir su contexto. |
| `causadb_feedback` | Lista eventos `HUMAN_FEEDBACK` del ledger. Para que el agente sepa qué feedback recibió manualmente. |
| `causadb_stream` | Lista eventos `STREAM_INTERRUPTED` del ledger. Para detectar sesiones anteriores que se cortaron. |

### Coordinación multi-agente

| Tool | Cuándo usarla |
|------|--------------|
| `shared_document_read(name)` | Leer `AUDIT_REPORT` o `ACTION_PLAN` (documentos fijos para coordinación Maker↔Checker). |
| `shared_document_write(name, content)` | Escribir/actualizar `AUDIT_REPORT` o `ACTION_PLAN` (JSON válido, se sobreescribe). |

---

## 3. Cómo reconstruir el estado previo (escalera de memoria)

Cuando entrás a un proyecto con CausaDB, la memoria está en 4 niveles, ordenados de barato a caro. **Empezá por el más barato y subí solo si no tenés suficiente contexto.**

1. **`causadb_revive`** (barato) — bootstrapping context: Chronicle + últimas 10 GOVERNANCE_DECISION + particiones OCB preloaded. Ya te dice el session type (first_run / abrupt_close / normal_close) y qué hace el OCB. **Usá este siempre primero, antes de leer el Chronicle completo o tocar cualquier otra cosa.**

2. **OCB particiones** (barato) — memoria granular de corto plazo. `causadb_ocb_status` te da el overview; `causadb_ocb_load_partition` carga una partición individual. El OCB es cache L1 volátil, particiones ya rotadas siguen ahí hasta purgue; el detalle granular (snapshots pre/post) está en el BlobStore y se carga bajo demanda.

3. **`causadb_query`** (medio) — filtrado puntual del ledger por `event_type`, `ctx_id`, `text`, rango temporal, `source`. Usá esto cuando sabés qué buscás: "todos los TRADE_EXECUTED de ayer", "todos los FILE_MODIFIED en este archivo". No cargues todo el ledger si buscás algo específico.

4. **`causadb_replay`** (caro) — reconstruye el estado completo del proyecto. Lee TODOS los eventos en orden y te da el snapshot final + métricas. Usá esto SIEMPRE como último recurso. Solo si revive/OCB/query no alcanzaron para entender el problema.

**Regla práctica:** si tuviste que llegar a `replay`, documentá por qué los niveles más baratos no alcanzaron — ayuda a la siguiente iteración de CausaDB a hacer mejor la escalera.

**Cómo está guardado el contenido (crítico para reconstruir):** los eventos `FILE_MODIFIED` del ledger capturan el **contenido íntegro** del archivo en `payload.content` junto a `content_hash` (SHA-256 del contenido) y `size`. Los campos `pre_snapshot`/`post_snapshot` del schema están en `None` — el snapshot vive en el `payload` del evento, no en un campo aparte. Cada modificación de un archivo es un snapshot completo de ese punto en el tiempo; el estado previo de un archivo es el `payload.content` del evento `FILE_MODIFIED` inmediatamente anterior en la cadena. Excepción: los binarios (tarballs, .db) no llevan `content` ni `content_hash` — solo `path`/`size`/`action`; para ellos la restauración requiere el backup en disco.

**Límite de atribución:** los `FILE_MODIFIED` vienen con `source=harvester:filesystem` y NO distinguen qué agente (opencode/gemini/claude) tocó el archivo. La atribución se resuelve cruzando los eventos `TOOL_CALLED`/`REASONING_STEP` de la sesión de la misma ventana temporal (`causadb_ocb_load_partition` de esa partición) o los docs de gobernanza del Chronicle.

---

## 4. Patrones de reconstrucción dirigida

La escalera (§3) ordena por costo, pero no dice **qué tool usar para cada pregunta concreta**. Estos patrones nacen de casos reales (sesión del 13-08-2026: auditoría de qué hizo el agente Maker gemini en la tarea H2.1).

### P1. "¿Qué hizo el agente X en la última sesión y en qué quedó?"
1. `causadb_revive` → últimas decisiones + particiones preloaded (qué sesión fue la última, si cerró normal o abrupto).
2. `causadb_ocb_status` → partición/particiones de esa sesión por `session_ids`/`sources`.
3. `causadb_ocb_load_partition` de esa partición → los `TOOL_CALLED`/`REASONING_STEP` en orden: qué comandos corrió, qué archivos leyó/edito, dónde se cortó.

*Caso real:* la partición `OCB_PARTITION_1786624998726052709.log` mostró en orden los 16 reads del coder H1 (lectura de `_harvest_source_hermes.py`, fixtures, `_replay_engine.py`…) con `abrupt_close` — sin abrir el repo, supimos exactamente en qué quedó y por qué.

### P2. "¿Qué archivo tocó, cuándo y con qué contenido?"
`causadb_query(event_type="FILE_MODIFIED", text="<path>")` → lista de eventos con `action` (created/modified), `timestamp`, `size`, `content_hash` y el **contenido íntegro** en `payload.content`. La secuencia ordenada por tiempo es la historia completa del archivo.

*Caso real:* `text="test_hermes_api_attempt"` devolvió 4 snapshots del test (created 18:07Z 1673B → modified 18:11Z → 18:18Z 4825B → 18:54Z 4416B) — la evolución exacta con el contenido de cada versión, incluyendo la reescritura con formato real tras el rechazo del Checker.

### P3. "Restaurar un archivo a su estado previo (por ejemplo: un agente tocó tests que no debía)"
1. `causadb_query(event_type="FILE_MODIFIED", text="<path>")` → tomá el evento cuyo contenido querés restaurar.
2. Escribí `payload.content` en disco.
3. Verificá: `sha256sum` del archivo == `content_hash` del evento. Si cuadra, la restauración es exacta.

*Caso real:* los tests que un agente tocó indebidamente se restauraron así — el estado previo es el `payload.content` del evento anterior en la cadena, no una copia manual. (Para binarios sin `content`, usar el backup en `backups/`.)

### P4. "¿Por qué existe esta línea? / ¿qué me llevó hasta acá?"
- `causadb_why(file_path, line_number)` → el evento que introdujo esa línea (causal blame).
- `causadb_trace(file_path, line_number)` → todo el cone causal upstream.
- `causadb_impact(event_id)` → el blast radius downstream (qué rompió).

### P5. "¿El agente metió mano donde no debía?"
`causadb_sandbox` → reconstruye el estado + lista de mutaciones fuera del área permitida (violations + total_mutations). Complementá con `causadb_score` para ver churn/waste/survival de una sesión.

### P6. "¿El DONE que reportó un agente es verdad?"
No confíes en el reporte: 
1. `causadb_query(event_type="FILE_MODIFIED", text="<archivo que dice tocar>")` → ¿existe el evento con el contenido que dice?
2. Cruzá con la suite real (`pytest`) y con `causadb_validate`/`causadb_sentinel`.
3. Si el reporte cita números (tests passed, líneas), verificá que esos números existan en los `COMMAND_RUN` o en una corrida tuya. Declarar DONE sin evidencia reproducible es teatro (Art. III).

*Caso real:* gemini reportó DONE de la tarea H2.1 con "5 tests PASS", pero contra el store real el parser emitía 0 eventos — los tests usaban log sintético sin el nivel `INFO` real. El chequeo `FILE_MODIFIED` + corrida real contra `/tmp/opencode/hermes-validate-home/` desenmascaró el teatro.

### P7. "¿Qué hicimos sobre X y por qué? / ¿qué BITs tocaron el mismo tema?"
0. **Para sedimentar un BIT nuevo** (no para buscar): usá `causadb_chronicle_append` (MCP) o `causadb chronicle append-md` (CLI) — template curado, idempotente y FAIL-CLOSED. NO edites el Chronicle a mano (ver Art. I/II).
1. `grep -n "X" CAUSADB_CHRONICLE.md` (o `rg -i "X"`) → encuentra el/los `## BIT-... — Título`.
2. `causadb chronicle list` para ver el índice completo con conteo de eventos (usá `--unlinked` para ver los que aún no tienen eventos enlazados).
3. `causadb chronicle events --bit <BIT>` → lista de `event_id` enlazados a ese BIT.
4. `causadb chronicle reconstruct --bit <BIT>` → estado reconstructible al momento de ese BIT (frontera por max seq en append-order).
5. Para BITs legacy sin eventos enlazados: `causadb query` por rango temporal del BIT o lectura directa del bloque en Chronicle.

*Caso real:* búsqueda de la doctrina de la escalera de recuperación (2026-08-14): `grep "escalera" CAUSADB_CHRONICLE.md` localizó de inmediato BIT-CHR.49 ("Fase R.4 — Reconstrucción guiada por bitácora y recuperación escalonada"), recuperando en segundos el diseño original de la escalera barata→cara sin tener que excavar en logs crudos.

### P8. "Auditar a un agente Maker por evidencia, no por confianza"
Cuando un subagente (Maker) implementó código y reportó DONE, auditalo desde la **traza causal** en lugar de revisar el repo a ciegas. Esto detecta teatro (modificar tests para que pasen, declarar DONE falso, conteos inventados) y violaciones de reglas de proceso (RED-first, alcance) por evidencia, no por lectura exhaustiva.
1. `causadb_ocb_status` → localizá la partición de la sesión del Maker por `session_ids`/`sources` (ej. `harvester:opencode`, `harvester:gemini`).
2. `causadb_ocb_load_partition` → los `TOOL_CALLED`/`FILE_MODIFIED`/`REASONING_STEP` en orden temporal: qué comandos corrió, qué archivos tocó, en qué secuencia.
3. Verificá proceso:
   - **Orden RED→GREEN (Art. III):** el test nuevo debe aparecer como `FILE_MODIFIED` ANTES de la corrida que lo hace pasar; si un test existente se modificó DESPUÉS de una corrida fallida, es sospechoso de teatro.
   - **Alcance:** qué archivos tocó vs. el contrato. Archivos fuera de alcance modificados = red flag.
   - **Claims vs evidencia:** si reportó "corrí N tests / pasaron M", contá los `TOOL_CALLED` de pytest y cruzá contra tu propia corrida real.
4. Confirmá el teatro leyendo SOLO los diffs de los archivos señalados (assert triviales, `pass` vacíos, hardcode de retorno).
5. Verificá aislamiento (Art. V): `causadb_why`/`causadb_trace` atribuyen líneas sospechosas al evento que las introdujo.

*Caso real:* auditoría del Maker H2.2 (2026-08-15). La traza mostró que el agente reportó DONE con la suite "1825 passed" ANTES de que el ledger registrara las corridas reales — el claim de DONE no coincidía con la evidencia de `TOOL_CALLED` de pytest. Reconstruyendo el orden de `FILE_MODIFIED` del test nuevo vs. la implementación se verifica TDD-RED real. El área de auditoría se reduce de "todo el repo" a "solo los archivos que el Maker tocó, en orden".

**Pitch de producto:** auditar por evidencia hace el flujo Maker-Checker más sano y barato — el Checker deja de leer el repo completo para adivinar qué cambió y pasa a verificar la traza (ahorro de tokens), detectando teatro por discrepancia de orden/alcance/claims en lugar de por inspección manual, y conservando la confirmación de contenido solo para los archivos señalados.

### P9. "¿Qué conversación llevó a esta decisión? / reconstruir la prosa de un BIT"

El ledger guarda el **qué** (eventos) y el **porqué estructurado** (GOVERNANCE_DECISION), pero la **prosa de la conversación** — el diálogo operador↔agente que llevó a la decisión — vive en el storage privado de cada herramienta de agente (`opencode.db` para opencode, `.jsonl` para gemini, etc.). Para reconstruirla, subí de barato a caro:

1. **`causadb_query(event_type="GOVERNANCE_DECISION", text="<BIT>")`** — el `reasoning` de la GOV enlazada ya condensa el porqué. Si alcanza, parás acá (barato).
2. **`causadb_revive` / `causadb_ocb_status`** — localizá la partición de la sesión en la ventana temporal del BIT (la fecha del BIT en el Chronicle acota el rango).
3. **Extraé el `session_id`** de cualquier `REASONING_STEP`/`TOOL_CALLED` de esa partición (`causadb_ocb_load_partition`) — el payload lleva el `session_id` de la sesión.
4. **`causadb recover <session_id> --tool <tool>`** (o tool MCP `recover`) — storyboard completo de la sesión desde la fuente cruda: turns con prompts del operador, respuestas del agente, reasoning y tool_calls en orden. 

> ⚠️ **Nota:** `recover` funciona de forma agnóstica para el usuario — auto-detecta la fuente o resuelve el provider vía `conversation_ref`. Soporta 9 herramientas: opencode, gemini, claude, cursor, windsurf, codex, grok, hermes, openjarvis. La limitación real es que la sesión debe existir en el storage de la herramienta que la originó (si fue purgada, no se recupera). El Plan E-Causal (auto-distill de decisiones) ya está implementado (`_decision_distill.py`); la indexación pasiva del storyboard completo (turns con prompts del operador) es un problema no resuelto.

*Caso real:* reconstrucción del porqué de BIT-CHR.7 (2026-08-17). `causadb_query` dio la GOV retrologueada (`c2bb478c`, seq 72); la partición OCB de la ventana 20:24-20:53Z dio los `REASONING_STEP`/`TOOL_CALLED` de la sesión principal con `session_id=ses_03b353695ffeyFqH0vwPIfnr9S`; `causadb recover ses_03b273f7affeS5I3s4QT9p19KC --tool opencode` devolvió el prompt completo del plan Fase 7 y los 34 turns del coder — sin leer `opencode.db` crudo. (El primer intento manual con queries sqlite ad-hoc costó ~15 tool calls; el patrón documentado acá lo reduce a 4.)

**Convención de redacción de BITs:** el título de cada entrada del Chronicle (`## BIT-CHR.N — <Título>`) DEBE contener los términos clave con los que un futuro agente buscaría la funcionalidad o decisión (la superficie de búsqueda léxica).

---

## 5. Init agnóstico: sembrar el puntero en el agente correcto

Cuando corrés `causadb init` (o `causadb setup`), CausaDB escribe un puntero en el archivo de reglas de tu agente (opencode → `AGENTS.md`, gemini → `GEMINI.md`, claude → `CLAUDE.md`, codex → `instructions.md`) con:

- el `ledger_path` del workspace,
- los casos de uso (auditar, ahorrar tokens, trazabilidad, reconstrucción de estado, recuperar archivos),
- cómo leer este documento: `causadb canon` (CLI) o el resource MCP `causadb://canon`.

**El puntero es agnóstico:** no depende de una URL externa ni de rutas de máquina — el canon viaja dentro del producto (`causadb/docs/canon.md`) y se mantiene una sola vez (doctrina BIT-49 / briefing:92). El agente lo lee solo cuando necesita alguno de los casos de uso listados. **La REGLA 1 (cierre de sesión → GOVERNANCE_DECISION) viaja en el puntero, no en el canon**: es comportamiento proactivo que depende 100% del agente (puede no llamar revive), y el archivo de reglas es lo único que se lee siempre al arrancar (decisión del operador 2026-08-14). **Cómo elegimos a qué agente sembrar:**

1. **`--agent {opencode,gemini,claude,codex}`** — override explícito. Gana siempre. **Usalo si tenés más de un agente instalado** (ej. opencode + gemini-cli): la heurística sola no puede saber quién tiró el comando.
2. **`CAUSADB_AGENT` env var** — si no pasás el flag, pero querés que todos los inits de una shell vayan a un agente específico. `export CAUSADB_AGENT=gemini`.
3. **Heurística** — si no hay flag ni env: primer archivo de reglas existente en orden `[opencode, gemini, claude, codex]`, luego binario en PATH. Último recurso; **en máquinas multi-agente no es confiable** (siempre gana el primero del hardcode).

**Por qué importa:** sin el flag correcto, la doctrina cae al archivo equivocado — viste opencode la línea, pero gemini-cli no (deuda #23, BIT-CHR.58). Un `--agent gemini` evita que un agente instale CausaDB y no se entere.

---

## 6. Doctrina de producto (3 Arts)

CausaDB regula lo que genera **trazabilidad** para que cumpla lo que promete — memoria determinista del pasado. No prescribe tu forma de trabajar (tests, runner, workflow son decisión tuya). Tres reglas de cómo interactuás con CausaDB son no negociables.

### Art. I — Ledger Monism

El ledger (`ledger.log`) es la **única** fuente histórica. No escribas directo a `ledger.log` con `open(..., "a")` — pasá siempre por `LedgerWriter.append` o por la tool MCP `causadb_log`. Lo mismo para decisiones: usá `causadb_log_decision`, no edits manuales al Chronicle que no pasen por el ledger.

**Enmienda (sedimentación narrativa vs. decisiones estructuradas):** hay DOS capas de sedimentación, y esta enmienda las distingue explícitamente:

- **Layer 1 — decisiones estructuradas al ledger** (técnica, inmutable): vía `causadb_log_decision` (MCP) o `causadb log --decision` (CLI). Es la fuente de verdad para `revive`/`query(source_type="agent")`.
- **Layer 3 — sedimentación narrativa al `.md`** (humana, curada): vía `causadb_chronicle_append` (MCP) o `causadb chronicle append-md` (CLI). Reemplaza el edit manual del agente sobre `CAUSADB_CHRONICLE.md` — template curado, idempotente (bit_id duplicado → `already_exists`), FAIL-CLOSED (sin chronicle resoluble o campos faltantes → error), con `flock` (concurrencia) y `fsync` (durabilidad).

La alineación ledger ↔ .md la garantiza el **`bit_id` compartido** + el **`event_id` opcional en `**Referencias:**`** (formato que `_PROSE_EVENT_ID_RE` captura en `_chronicle_index.py`). El workflow Maker-Checker exige que TODO BIT tenga su GOV linked (`causadb_log_decision` con `bit=<BIT>`).

Esta enmienda **revierte la recomendación del audit legacy `docs/_legacy/AUDIT_SETUP_CHRONICLE.md:40-43`** ("`causadb chronicle append` es riesgoso — un append ciego puede romper el markdown; appendar al chronicle sigue siendo manual"). El append al `.md` ahora es seguro por template curado + idempotencia + FAIL-CLOSED + flock + fsync — el riesgo que motivó el audit (append ciego rompiendo la estructura) queda cubierto por diseño.

La hash-chain es inmutable: si alguien reordena, edita, o inventa eventos, `causadb_validate` lo detecta. No intentes "corregir" el ledger escribiendo en el pasado — appendás al final con un nuevo evento que documente la corrección.

### Art. II — Sedimentá decisiones

Cambios arquitecturales, fixes no triviales, decisiones estratégicas se asientan con `causadb_log_decision` (vía MCP) y con un BIT-entry en `CAUSADB_CHRONICLE.md` (append-only). El BIT-entry se asienta vía `causadb_chronicle_append` (MCP) o `causadb chronicle append-md` (CLI) — template curado, idempotente y FAIL-CLOSED; NO edites el Chronicle a mano. Las decisiones son lo que más se pierde entre sesiones: si no sedimentás por qué cambiaste algo, el próximo agente va a tener que adivinar.

#### REGLA 1 — cierre de sesión (sedimentada 2026-08-11)

Al terminar una sesión de trabajo (o al pausar con trabajo pendiente), el agente DEBE loguear un evento `GOVERNANCE_DECISION` (decision_type="tactical", impact="low", origin="agent") cuyo `reasoning` documente: (a) qué se hizo en la sesión, (b) qué queda pendiente/definido para la próxima sesión.

Motivo: las intenciones de sesión NO viven en transcripts harvesteados (blobs opacos) sino en eventos estructurados de intención, accesibles vía `revive`/`query(source_type="agent")`. Sin esto, la siguiente sesión no puede recuperar contexto barato (lección aprendida 2026-08-11: el Fix 2 "flatten/roundtrip" definido en sesión previa quedó solo en REASONING_STEP/TOOL_CALLED con `$blob`, imposible de recuperar sin resolver blobs).

#### Convención: "Prosa de Sesión" en cada BIT-entry (cierre deuda #17 / BIT-CHR.40)

El ledger captura **qué** pasó (eventos, decisiones) y **qué** decidiste y por qué (GOVERNANCE_DECISION). Lo que NO captura es la **prosa de la conversación** — las dudas, explicaciones narrativas y el razonamiento dialéctico que te llevó a la decisión. Esa prosa vive hoy en el storage privado de cada agente (`opencode.db`, chat de Windsurf, etc.), inaccesible para quien no use ese agente.

Para cerrar ese hueco de forma **económica** (sin transcript harvesting completo), cada BIT-entry en `CAUSADB_CHRONICLE.md` debe incluir un bloque **`**Prosa de Sesión:**`** de 2-3 líneas narrativas que respondan:

- ¿Por qué elegimos este enfoque (y no las alternativas)?
- ¿Qué dudas surgieron y cómo se resolvieron?
- ¿Qué trade-offs aceptamos?

Reglas de la convención:

1. **El Maker del cambio** (el agente que cierra la decisión) es responsable de redactarlo. Es la entidad que vivió la conversación — la mejor posicionada para condensar el "por qué" sin alucinar.
2. **No edites el pasado.** No tiene sentido reconstruir la prosa de BITs viejos retroactivamente (propenso a alucinación, Art. IX). La convención aplica a BITs futuros.
3. **El Ledger NO se toca.** La narrativa va en el Chronicle (Layer 3, humana), nunca en `ledger.log` (Layer 1, inmutable y técnica). La cadena causal sigue pura.
4. **Sé económico.** 2-3 líneas, no el transcript. Si la prosa no aporta contexto de decisión, un puntero a la discusión alcanza.
5. **El próximo agente que complete un BIT se fija en el último** para replicar el formato — por eso el bloque es parte del template, no opcional-soft.

El resultado: un humano o agente futuro que lea el Chronicle entiende el contexto de la decisión sin abrir el storage del agente que la tomó.

No dejes decisiones críticas en docs sueltos. Si la decisión es importante, viví en el ledger + Chronicle. Si vivía en un doc, move esa info al Chronicle.

### Art. III — No declares false DONE

No asientes en el Chronicle ni en eventos del ledger que algo ocurrió si no ocurrió. El ledger es auditable: `causadb_validate`, `causadb_sentinel`, `causadb_score`, `causadb_sandbox` detectan incongruencias. Trazabilidad = verdad que coincide con lo que el ledger dice.

Específicamente:
- Si decís "fixeado" en una decisión, el fix tiene que estar en el ledger como evento FILE_MODIFIED.
- Si decís "1631 passed/0 failed" en un BIT-entry, eso tiene que ser verdad — el próximo agente lo puede correr y verificar.
- Si decís "audité los 3 tests RED", esos tests tienen que existir con los nombres que citaste.

Declarar DONE sin evidencia es teatro. El ledger y la suite te van a desengañar al primer intento de auditoría.

---

## 7. Queda fuera del canon

El canon de CausaDB es doctrina de **producto** — cómo interactuás con CausaDB para que cumpla lo que promete. No es doctrina de proceso de desarrollo.

Lo que CausaDB **no prescribe**:
- Tu runner de tests (pytest, go test, cargo test, ruff — decisión del codebase que uses)
- Tu workflow de desarrollo (TDD, code review, pair programming, Maker-Checker — decisión tuya)
- Tu estilo de commits, tu stack, tu lenguage favorito, tu editor.

CausaDB te ayuda a tener memoria del pasado. Cómo desarrollás en el presente es decisión tuya y del equipo en el que trabajás.

---

## Apéndice: breve referencia

- 19 tools MCP disponibles: enumeradas arriba.
- 20 fuentes de harvest pasivo (`_harvest_source_*.py`): shell, git, browser, ActivityWatch, MT5, Jupyter, Obsidian, Zotero, filesystem, n8n, Freqtrade, Cursor, Windsurf, opencode, Claude, Gemini, Codex, Hermes, OpenJarvis, Grok. TradingView via adapter webhook (`adapters/tradingview/`), no pasivo.
- Documentación operational completa: `docs/user_guide.md`, `docs/faq.md`, `docs/troubleshooting.md` (específica para CLI, no para canon).
- Chronicle del proyecto donde laburás: ver `CAUSADB_CHRONICLE.md` en el root del repo para BIT-entries históricos. Cada BIT nuevo debe incluir el bloque `**Prosa de Sesión:**` (ver Art. II).
- Roadmap y estado del producto: `CAUSADB_STATE_AND_ROADMAP.md`.
- Init multi-agente: `causadb init --agent gemini` (o `CAUSADB_AGENT=gemini`) para sembrar la doctrina en el agente correcto (ver §5).

---

## 8. Coordinación Multi-Agente

Dos mecanismos: **documentos compartidos** (estado即时 entre agentes) y **skills procedurales** (patrones de uso de las tools de CausaDB).

### 8.1 Documentos compartidos

Dos anotadores fijos en `.causadb/coordination/`:

- **AUDIT_REPORT** — escribe solo el Auditor/Checker. Estados: BORRADOR/APROBADO/RECHAZADO/REQUIERE_CAMBIOS.
- **ACTION_PLAN** — escribe solo el Coder/Maker. Estados: solicitud APROBAR/OBJETAR.

Se sobreescriben (no son historial — el ledger guarda el historial completo vía `FILE_MODIFIED`).

Tools: `shared_document_read(name)`, `shared_document_write(name, content)` (JSON válido).

**Workflow típico Maker↔Checker:**
1. Maker escribe su plan en `ACTION_PLAN`
2. Checker lee `ACTION_PLAN`, verifica, escribe veredicto en `AUDIT_REPORT`
3. Maker lee `AUDIT_REPORT`, ejecuta o ajusta
4. Al cierre, el ledger tiene la traza completa de la coordinación

### 8.2 Skills procedurales

Skills predefinidas que condensan patrones de uso de CausaDB. Se inyectan en el setup y se pueden listar con `causadb_skill_list`:

| Skill | Qué hace | Disparador típico |
|-------|----------|-------------------|
| `state-reconstruction` | 9 patrones P1-P9 para reconstruir estado desde el ledger | "¿Qué hizo el agente X?", "¿Por qué existe esta línea?", "Restaurar este archivo" |
| `shared-workspace` | Coordinación multi-agente via shared documents | "Leer el plan de acción", "Escribir reporte de auditoría" |

Las skills son 100% agnósticas — solo referencian tools CausaDB, no dependen de ninguna herramienta de agente específica.
