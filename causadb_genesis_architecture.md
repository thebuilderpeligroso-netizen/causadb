# Arquitectura de Ingesta Génesis: Onboarding Inteligente

## Visión
La 'Era Génesis' es una estrategia de onboarding que permite a CausaDB ser productivo desde el minuto uno, inyectando un contexto histórico reconstruido mediante la destilación de artefactos existentes (Git, Obsidian, Docs, Codebase), sin contaminar la trazabilidad nativa del sistema.

## Conceptos Clave
1. **Provenance:** Todos los eventos generados en el Génesis están marcados explícitamente con `provenance: "genesis"`. Esto permite que el agente y el sistema diferencien entre información inferida/reconstruida (Génesis) y eventos observados nativamente por el daemon (Live).
2. **Resumen Destilado:** El sistema no inyecta el grafo completo ni los archivos brutos en el contexto. Se genera un único `GENESIS_SUMMARY` compacto y se exponen punteros a los datos brutos (en BlobStore/Ledger) para consulta bajo demanda.
3. **Multi-Fuente Semántica:** El Génesis cruza información heterogénea para generar una síntesis arquitectónica:
    - **Git:** Evolución y antigüedad.
    - **Codebase:** Estructura, dependencias, hotspots.
    - **Obsidian:** Intención, decisiones, problemas conocidos.
    - **Docs:** Contratos y arquitectura declarada.

## Flujo de Inicialización (causadb init)
1. **Escaneo (Sources):** Git, Docs, Obsidian, Codebase, Filesystem.
2. **Destilación (Genesis Inputs):**
    - `CODEBASE_ARCHITECTURE_SNAPSHOT` (Codebase)
    - `GENESIS_SUMMARY` (Síntesis cruzada con provenance)
3. **Sedimentación (Ledger + Chronicle):** Registro inmutable de la Era Génesis.
4. **Bootstrapping (Revive):** El agente recibe un resumen de alta densidad al iniciar, pudiendo profundizar bajo demanda.

## Reglas de Integridad
- **No duplicación:** El Génesis es una operación batch de bootstrap, no un parche de eventos históricos falsos.
- **Transparencia:** El usuario debe saber qué es parte del Génesis y qué es observación nativa.

## Identidad de Proyecto (PROJECT_ID)

Para soportar la evolución comercial y técnica (multi-ledger, auditoría y gobernanza), se formaliza la identidad persistente del proyecto:

1. **Persistencia:** Al ejecutar `causadb init`, se genera un `PROJECT_ID` único (UUIDv4) que se guarda en `.causadb/config.json`.
2. **Asociación Implícita:** El daemon y las herramientas de CLI utilizan este ID como identificador inmutable en todas las consultas y registros, permitiendo diferenciar contextos incluso si comparten rutas en el sistema de archivos.
3. **Escalabilidad:** Esta identidad permite futuras implementaciones de:
    - **Licenciamiento por Proyecto:** Asociación de un ID a un plan comercial.
    - **Gestión Multi-Proyecto:** El daemon podrá catalogar múltiples proyectos mediante sus IDs sin necesidad de brokers complejos de registro global, manteniendo el aislamiento y la independencia.

## Plan de Implementación (Fases)

Para maximizar la reutilización de código (harvesters existentes) y asegurar una integración suave, el plan se divide en tres fases incrementales:

1. **Fase 1: Motor Génesis (CLI)**: Creación de la CLI `causadb genesis import --source <name>`. Reutiliza los `harvesters` existentes (ej: `GitReflogSource`, `FilesystemSource`) para realizar una ingesta *one-shot* de eventos históricos marcados como `GÉNESIS_IMPORT` en el ledger. No inicia servicios de background ni timers.
2. **Fase 2: Identidad de Proyecto (PROJECT_ID)**: Implementación de la persistencia del UUID en `.causadb/config.json` durante el `init`. Esta fase establece la base inmutable para el futuro licenciamiento, auditoría y multi-proyecto.
3. **Fase 3: Integración en Onboarding (UX)**: Flujo de usuario al finalizar `causadb init`:
   - *"¿Vas a usar CausaDB en un proyecto ya comenzado? (Y/N)"*
   - Si "Y": Ejecución resiliente de la Fase 1 (escanea y analiza las fuentes disponibles). Si no detecta fuentes, continúa sin bloqueo.


## Estrategia de Validación (Empírica)

Para asegurar la calidad y fidelidad de la memoria reconstruida, se utilizará un protocolo de validación basado en datos reales de un proyecto maduro:

1. **Backup**: Se realiza una copia completa del ledger actual (`.causadb/`).
2. **Aislamiento**: Se elimina el ledger original del alcance del daemon (`mv .causadb .causadb.backup`).
3. **Reconstrucción**: Se ejecuta `causadb init` seguido de `causadb genesis import --all`.
4. **Contraste**: Se comparan las métricas clave (eventos totales, hitos críticos, commits rastreados, decisiones identificadas) entre el `ledger.log` reconstruido y el backup.
5. **Criterio de éxito**: La reconstrucción debe ser capaz de alimentar el `revive` con suficiente contexto para que el agente responda preguntas operativas básicas sobre el pasado del proyecto con una fidelidad superior al 80% respecto a la experiencia original.

