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

CausaDB expone un **MCP server con 19 tools** (incluye `recover` para reconstruir el storyboard completo de una sesión desde la fuente cruda) que cualquier agente compatible invoca en 1 segundo:

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

**En preparación para la primera release pública.** Roadmap y estado actual en `CAUSADB_STATE_AND_ROADMAP.md`. Validación pre-lanzamiento en `../CAUSADB_VALIDATION_CHECKLIST.md`.

> **Nota de transparencia:** el repositorio público de GitHub y los instaladores binarios todavía no están publicados. Forman parte del plan de lanzamiento. No usés links de "releases" de versiones anteriores del README — apuntan a un repo que aún no existe.

## 📚 Documentación

- [Guía de Usuario](docs/user_guide.md) — Primeros pasos, instalación, comandos útiles (español)
- [Preguntas Frecuentes](docs/faq.md) — FAQ sobre privacidad, precios, errores comunes
- [Solución de Problemas](docs/troubleshooting.md) — Errores típicos y cómo resolverlos
- [Verificación de Firmas](docs/verify_signature.md) — Cosign keyless para binarios
