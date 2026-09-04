# CausaDB: La memoria de largo plazo para tus agentes de IA

### Tu agente ya no olvida. Y vos ya no perdés horas de trabajo.

¿Alguna vez te pasó? Estás trabajando con un agente, le das instrucciones, te sumergís en el código... y de repente, **todo se apaga**.

Un bucle infinito, un corte de luz, una sesión cerrada por error. Y cuando volvés, el agente es un extraño: perdió el contexto, olvidó los matices críticos, y te toca empezar de cero. Lo intentás "rehidratar", pero ya no recuerda lo que era obvio hace 20 minutos.

**CausaDB existe para que eso sea cosa del pasado.**

---

## ¿Qué es CausaDB?

CausaDB es la **Caja Negra** que asegura la **Continuidad Cognitiva** de tus agentes de IA. Registra todo lo que hace tu agente (archivos, comandos, decisiones, razonamiento), lo protege con una cadena criptográfica, y **lo revive exactamente donde quedó** cuando lo necesitás.

- **Autonomía real:** Dejá a tu agente trabajando solo sin miedo. Si algo falla mientras no estás, CausaDB captura la causa raíz.
- **Memoria de 3 capas, como el cerebro humano:**
  - **Largo plazo:** el ledger inmutable (hash-chain criptográfica, append-only).
  - **Mediano plazo:** el working set reconstruible por replay determinista.
  - **Corto plazo:** el OCB (memoria de sesión) con detalle granular de archivos (snapshots pre/post).
- **Tu mejor complemento:** capa invisible sobre tus herramientas favoritas. Funciona con **modelos locales (Ollama, LM Studio)** y **en la nube (OpenAI, Claude)**.
- **Resiliencia total:** sobrevive a crashes, cortes de luz y cierres abruptos.
- **Trazabilidad completa:** no solo logs — una cadena completa (LLM → Razonamiento → Herramienta → Resultado).

### Caso de uso estrella: el "revive"

Apagaste la máquina, te fuiste, o se te cortó la luz. A la vuelta:

```bash
causadb revive
```

CausaDB reconstruye el estado, resume lo que pasó, y le devuelve al agente (o a vos) el contexto completo: **qué eventos hubo, qué había dentro de cada archivo, qué se decidió y por qué**.

---

## ¿Listo para dejar de rehidratar a tu IA?

No dejes que tu agente sea un ente volátil que se esfuma al primer problema.

[Instalar en 1 minuto](#instalación) · [Documentación](docs/user_guide.md)

---

## Instalación

```bash
pip install causadb
```

Sin Python instalado (compliance officers, traders, estudiantes): próximo binario standalone para Linux/macOS/Windows (en preparación para la primera release pública).

### Setup inicial (una vez por proyecto)

```bash
causadb setup /mi/proyecto          # init + hooks + vigilante + daemon, todo en uno
```

### Trabajo diario

```bash
causadb watch start --workspace /mi/proyecto --daemon   # vigilante + mcp-proxy + proxy LLM
causadb revive                       # reconstruye estado + resume de contexto
causadb trace /ruta/archivo.py 42    # ¿quién escribió esta línea?
causadb impact --event-id <id>       # ¿qué rompo si revierto este evento?
causadb why archivo.py:42            # atribución causal de la línea
causadb score                        # ¿qué tan productiva fue la sesión? (0-100)
causadb audit                        # ¿cuánto código sobrevive en git? (anti-teatro)
causadb bisect --test "pytest tests" # evento exacto que introdujo un bug
causadb dashboard                    # visualización web completa
causadb watch stop                   # genera score + skills automáticamente
```

### Integración con agentes (MCP)

CausaDB expone un **MCP server con 21 tools + 4 recursos** (incluye `recover` para reconstruir el storyboard completo de una sesión desde la fuente cruda) que cualquier agente compatible invoca en 1 segundo:

```bash
causadb opencode-config --project /mi/proyecto
```

Esto genera `causadb.opencode.jsonc`. Agregalo a tu `opencode.jsonc`:

```jsonc
{
  "mcp": {
    "causadb": {
      "type": "local",
      "command": ["python", "-m", "causadb.mcp.server"],
      "enabled": true,
      "environment": {
        "CAUSADB_LEDGER_PATH": "/mi/proyecto/.causadb/ledger.log"
      }
    }
  }
}
```

#### Exponer el MCP por HTTP (agentes remotos)

Además del transporte local (stdio), el MCP server se puede exponer por **HTTP (streamable-http)** para que un agente remoto (por ejemplo, en la nube) consulte la memoria del proyecto de forma segura:

```bash
causadb-mcp --transport streamable-http --host 127.0.0.1 --port 8000 --ledger /mi/proyecto/.causadb/ledger.log
```

Diseñado con seguridad por defecto:
- **Bind-safety:** sin API key configurada (`CAUSADB_MCP_API_KEY`), se niega a escuchar en interfaces no-loopback. Sin key, solo tu máquina.
- **Subconjunto de lectura:** expone solo `revive`, `query`, `ocb_status`, `validate`, `sentinel` y `shared_document_read` — no las tools de escritura (`log`, `shared_document_write`) ni las que exponen todo el contenido (`replay`, `state`).
- **Coordinación agnóstica:** un agente remoto puede leer el plan de coordinación (`AUDIT_REPORT` / `ACTION_PLAN`) que otro agente escribió en tu máquina — la memoria de coordinación pertenece al proyecto, no al agente.
- **Redacción:** los datos sensibles se redactan antes de devolverse.
- **Agnóstico al cliente:** la misma interfaz sirve para OpenCode, Claude, Gemini CLI y agentes remotos compatibles con MCP.

### Adaptable a cualquier agente — aunque no hable MCP

CausaDB no te obliga a cambiar de herramienta: **es CausaDB la que se adapta a tus agentes**, no al revés.

- **Agentes estándar (MCP):** OpenCode, Claude, Codex, Cursor y similares se conectan con el MCP server en un comando (ver arriba).
- **Agentes sin MCP** (agentes conversacionales, skills, plugins): CausaDB se instala como una herramienta nativa más. Sin tocar el "cerebro" del agente — se deja un archivo de skill en su carpeta de habilidades y se habilita con una sola línea de configuración.

**Caso real — OpenJarvis:** un agente conversacional que tiene acceso a tu proyecto, conversa con vos sobre mejoras e ideas, busca en internet y refina tus prompts. CausaDB le agrega, en 1 minuto, una herramienta de **solo lectura** con la que puede:

- pedir el `revive` (resumen de contexto para retomar el trabajo),
- auditar la memoria (`query`, `validate`, `sentinel`),
- responder "¿quién escribió esta línea?" (`why`) o "¿qué depende de qué?" (`trace`),
- consultar el estado de las sesiones (`ocb status`).

**Lo que hace a esto poderoso:** la memoria de CausaDB es **una sola por proyecto**. Todos tus agentes comparten la misma historia: lo que hizo OpenJarvis, OpenCode o cualquier otro queda en el mismo ledger, y cualquiera puede consultarlo. La información se comparte por el ledger, no por el agente.

> **Lección de instalación:** la herramienta lee el ledger al que está conectado el proyecto. Si la historia vive en el proyecto principal, instalá CausaDB desde ahí (`causadb init` en esa carpeta) para que el agente audite la historia real — nunca la instales en una carpeta general vacía.

### Coordinación Multi-Agente

Cuando varios agentes trabajan sobre el mismo proyecto (Maker↔Checker, subagentes, equipos), CausaDB expone **dos anotadores compartidos** en `.causadb/coordination/`:

- **`AUDIT_REPORT`** — escribe el Auditor/Checker. Estados: `BORRADOR` / `APROBADO` / `RECHAZADO` / `REQUIERE_CAMBIOS`.
- **`ACTION_PLAN`** — escribe el Coder/Maker. Estados: solicitud `APROBAR` / `OBJETAR`.

Se sobreescriben (el historial completo lo guarda el ledger vía `FILE_MODIFIED`). Se acceden con las tools MCP `shared_document_read` / `shared_document_write`.

**Flujo típico:** Maker escribe su plan en `ACTION_PLAN` → Checker lee, verifica y escribe el veredicto en `AUDIT_REPORT` → Maker ejecuta o ajusta. La traza completa de la coordinación queda en el ledger.

### Skills procedurales

CausaDB incluye skills predefinidas que condensan patrones de uso de sus propias tools:

| Skill | Qué hace | Disparador típico |
|-------|----------|-------------------|
| `state-reconstruction` | 9 patrones (P1–P9) para reconstruir estado desde el ledger | "¿Qué hizo el agente X?", "¿Por qué existe esta línea?", "Restaurar este archivo" |
| `shared-workspace` | Coordinación multi-agente vía documentos compartidos | "Leer el plan de acción", "Escribir reporte de auditoría" |

Se listan con `causadb_skill_list` (MCP) o `causadb distill`. Son 100% agnósticas — solo referencian tools de CausaDB, no dependen de ninguna herramienta de agente.

### Documentación (canon)

CausaDB incluye un **canon**: la doctrina mínima para que un agente (o un humano) interactúe correctamente con la memoria del proyecto — escalera de reconstrucción barato→caro, patrones de auditoría por evidencia (P1–P9) y reglas de gobernanza. Se lee con:

```bash
causadb canon          # CLI
```

o el resource MCP `causadb://canon`. Se referencia automáticamente en el `revive` y en el setup de cada agente.

### Dashboard web

Visualización completa sin consola: línea de tiempo humanizada, búsqueda, trace visual de causalidad, botón de revive, export de auditoría y métricas de sesión.

---

## Lo que CausaDB resuelve

| Herramienta | Qué problema resuelve |
|---|---|
| `revive` | El agente vuelve a la vida con todo el contexto. No arranca de cero. |
| `trace` / `why` / `impact` | ¿Quién tocó esta línea? ¿Qué rompo si revierto este cambio? |
| `snapshot` + auto-archivo | Fotos pre/post de cada archivo — memoria granular del "qué había dentro". |
| `resume` | El agente nuevo recibe el contexto de la sesión anterior. |
| `score` | ¿Mi sesión fue productiva? 0-100 midiendo churn, waste y supervivencia de código. |
| `skills` / `distill` | Patrones de trabajo aprendidos entre sesiones, reutilizables. |
| `bisect` | Encontrar el evento exacto que introdujo un bug. |
| `audit` / `audit-trail` | Audibilidad completa (EU AI Act, NIST AI RMF). |
| `sentinel` / `validate` | Integridad del ledger — ¿se corrompió? ¿hay eventos inconsistentes? |
| `watch --daemon` | El agente trabaja solo mientras vos no estás. Captura automática LLM + archivos + comandos. |
| `undo` | Restaurar un archivo desde el último snapshot conocido bueno. |
| 21 fuentes de harvest | Captura pasiva: shell, git, browser, ActivityWatch, MT5, Jupyter, Obsidian, Zotero, agentes de coding, n8n, Freqtrade y más. |

### Comandos CLI (35+)

`config`, `init`, `setup`, `chronicle`, `log`, `replay`, `sentinel`, `validate`, `query`, `feedback`, `vigilante`, `proxy`, `proxy-server`, `sandbox`, `stream`, `export`, `import`, `compliance`, `incident`, `audit-trail`, `mcp-proxy`, `serve`, `harvest`, `recover`, `watch`, `opencode-config`, `audit`, `ocb`, `impact`, `bisect`, `why`, `trace`, `resume`, `score`, `undo`, `snapshot`, `crash`, `update`, `user`, `sync`, `distill`, `explain`.

### Multi-plataforma

- **Linux:** soporte completo (double-fork nativo).
- **macOS:** idéntico a Linux.
- **Windows:** modo sin fork (subprocess). Degradación suave. *(Validación en máquina real pendiente — ver checklist pre-lanzamiento.)*

### Estructura de archivos

```
/mi/proyecto/
  .causadb/
    ledger.log           # ledger causal (hash-chain, append-only)
    dag.json             # cache DAG para trace/impact O(1)
    CAUSADB_CHRONICLE.md # bitácora narrativa (append-only)
    pids/                # PID files de daemons
    logs/                # logs de daemons y proxy
    blobs/               # snapshots y blobs content-addressed
    ocb/                 # memoria de sesión particionada (OCB L1)
    skills/              # cache reconstructible de skills (ledger-first)
  causadb.opencode.jsonc # template MCP para OpenCode
```

### Requisitos del sistema

- Python 3.10+
- Linux, macOS, Windows
- Sin dependencias externas (todo stdlib)
- Vigilante (file watcher): `pip install watchdog` (opcional)

---

## Estado del proyecto

**En preparación para la primera release pública (v0.2.0-rc1).** Suite de ~2.135 tests, validación multi-plataforma en curso. Ver [releases](https://github.com/thebuilderpeligroso-netizen/causadb/releases) cuando estén publicados.

> **Nota de transparencia:** este repositorio es la fuente oficial. La primera release pública (`v0.2.0-rc1`) aún no está taggeada — está en validación (incluida la prueba en Windows). El paquete pip `causadb` y los binarios standalone se publicarán junto con esa release.

## Génesis: empezar en un proyecto ya comenzado

CausaDB funciona con la mayor fidelidad cuando se instala **desde el día 1** de un proyecto. Si lo instalás al comienzo, registra cada archivo, comando, decisión y razonamiento en el momento en que ocurren — la historia completa queda en el ledger.

Si ya tenés un proyecto **con meses de historia** y recién ahora lo incorporás a CausaDB, el **Génesis** reconstruye el contexto que puede recuperar de lo que ya existe: la estructura del código, los commits de git, los archivos y las notas de Obsidian. Es una reconstrucción **robusta pero no completa**: lo que pasó en conversaciones de agentes o procesos que no dejaron rastro en disco no se puede recuperar (ese detalle está en la sección [Limitaciones Conocidas](#limitaciones-conocidas)).

En criollo:

> **Un proyecto puede sobrevivir tres meses sin CausaDB. Pero un proyecto con CausaDB instalado desde el día 1 no solo sobrevive — se vuelve más confiable que cualquier otro proyecto.**

La memoria completa y verificable es el valor diferencial: cuanto antes lo instalás, más historia fiel y trazable acumulás.

## 📚 Documentación

- [Guía de Usuario](docs/user_guide.md) — Primeros pasos, instalación, comandos útiles (español)
- [Preguntas Frecuentes](docs/faq.md) — FAQ sobre privacidad, precios, errores comunes
- [Solución de Problemas](docs/troubleshooting.md) — Errores típicos y cómo resolverlos

---

## Limitaciones Conocidas

Transparencia sobre los límites actuales del producto, en orden de impacto:

1. **Génesis reconstruye estructura, no conversaciones.** Si incorporás un proyecto con historia ya comenzada, el [Génesis](#génesis-empezar-en-un-proyecto-ya-comenzado) reconstruye la estructura (archivos, commits git, Obsidian) con alta fidelidad, pero **no puede recuperar conversaciones de agentes ni decisiones que solo viven en el storage privado de las sesiones pasadas**. La fidelidad completa solo se alcanza instalando CausaDB desde el día 1.

2. **Atribución de identidad parcial (¿quién tocó qué?).** El ledger registra **qué** cambió siempre (vía harvester pasivo), y **quién** cuando el cambio pasa por un agente que firma su `session_id`. Pero los **borrados del filesystem watcher** (`source="harvester:filesystem"`) no llevan actor: el contenido borrado es irrecuperable y no se atribuye a nadie. La correlación `TOOL_CALLED ↔ FILE_MODIFIED` y la firma HMAC (`_attribution.py`) existen pero no están activadas en producción. Está en el roadmap como deuda #22.

3. **`undo` frente a estados intermedios rotos.** `undo` restaura al último estado que difiere del contenido actual en disco (cruzando disco + ledger). Si el historial contiene un snapshot intermedio "roto" seguido de uno bueno, `undo` puede no elegir automáticamente el último realmente válido — no valida el contenido del código. Para esos casos, el [canon](docs/canon.md) documenta el ritual manual de restauración (patrón P3).

4. **Windows en validación.** El soporte de Windows degrada a modo sin fork (subprocess). La validación en máquina real con la CI multi-plataforma está en curso antes de la primera release.

---

*Última actualización: 04/09/2026*
