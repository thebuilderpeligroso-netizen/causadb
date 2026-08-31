# Design Index — Mapa histórico de planes

Índice de referencia que mapea los planes históricos (PLAN_*.md, archivados
o borrados) a los BIT-entries del Chronicle y archivos de implementación.
Los docstrings del código citan `PLAN_X §N`; este índice resuelve esa
referencia a su implementación vigente.

Doctrina: el Chronicle (`CAUSADB_CHRONICLE.md`) es fuente histórica autorizada;
este índice es un helper de navegación, no un documento de diseño vivo.

## Planes archivados (5 en docs/_legacy/)
| Plan | Archivado porque | Info vivida en |
|------|-------------------|----------------|
| CAUSADB_GUIDE.md | Desactualizada (no menciona OCB, revive) | docs/user_guide.md + futuro docs/canon.md |
| CAUSADB_VISION.md | Visión original histórica | BIT tempranos en Chronicle |
| CHANGELOG.md | Duplica Chronicle | BIT-* en CAUSADB_CHRONICLE.md |
| AUDIT_SETUP_CHRONICLE.md | Auditoría puntual histórica | BIT-entry correspondiente en Chronicle |
| PLAN_DECISION_MINER.md | Diseño aprobado BIT-DM.1, implementación pendiente | BIT-DM.1 (l.2060) — deuda #19 |

## Planes borrados (13 — info vivida en Chronicle)
| Plan §N | BIT-CHR.M | Implementación .py |
|---------|-----------|---------------------|
| PLAN_HARVEST_MARKETS §Fase 1 | BIT-HM.1 (l.2849) | `causadb/_harvest_source_hermes.py` |
| PLAN_HARVEST_MARKETS §Fase 2 | BIT-OJ.1 (l.2868) | `causadb/_harvest_source_openjarvis.py` |
| PLAN_HARVEST_MARKETS §Fase 3 | BIT-CL.1 (l.2884) | `causadb/_harvest_source_claude.py` |
| PLAN_HARVEST_MARKETS §Fase 4 | BIT-GK.1 (l.2902) | `causadb/_harvest_source_grok.py` |
| PLAN_HARVEST_MARKETS §12 | StoryBoard persistente | `causadb/_storyboard.py` |
| PLAN_HARVEST_MARKETS §13 | Recovery de sesiones | `causadb/_recover_session.py`, `cli/_cmd_recover.py` |
| PLAN_HARVEST_MARKETS §15.2-ter | BIT-CHR.16 Codex | `causadb/_harvest_source_codex.py` |
| PLAN_HARVEST_MARKETS §15.4 | BIT-CHR.17 n8n | `causadb/_harvest_source_n8n.py` |
| PLAN_HARVEST_MARKETS §15.5 | BIT-CHR.18 Freqtrade | `causadb/_harvest_source_freqtrade.py` |
| PLAN_HARVEST_MARKETS §15.2-bis | BIT-CHR.20 Windsurf | `causadb/_harvest_source_windsurf.py` |
| PLAN_HARVEST_MARKETS §15.9 | BIT-CHR.18 TRADE_EXECUTED spec | `causadb/_harvest_source_freqtrade.py:62` |
| PLAN_HARVEST_AGENTES §5.1 | Motor universal de transcripción | `causadb/_agent_transcript.py` |
| PLAN_HARVEST_AGENTES §5.2 | Puntita gemini | `causadb/_harvest_source_gemini.py` |
| PLAN_HARVEST_AGENTES §5.3 | Puntita opencode | `causadb/_harvest_source_opencode.py` |
| PLAN_HARVEST_AGENTES §5.4 | Wiring del daemon | `causadb/_daemon_service.py:127`, `cli/_cmd_serve.py:5,44`, `cli/_cmd_harvest.py:6`, `cli/main.py:43,357`, `cli/_cmd_watch.py:98`, `_rest_api.py:926` |
| PLAN_HARVEST_AGENTES §6 | Auditoría I.2 aislamiento | `causadb/_harvester.py:156,192` |
| PLAN_OCB_BLOBSTORE F0 | BIT-CHR.36 (l.3984) F0 OCB↔BlobStore | `causadb/_ocb_manager.py`, `_harvester.py`, `cli/_cmd_resume.py`, `cli/_cmd_ocb.py` |
| PLAN_OCB_GAPS_CLOSURE F1 | BIT-CHR.37 (l.4047) tools MCP OCB | `causadb/mcp/_tools.py:ocb_status`, `ocb_load_partition` |
| PLAN_MULTI_MARKET pt A | `EventRegistry` pluggable | `causadb/_event_registry.py` |
| PLAN_MULTI_MARKET pt B | (ver Chronicle BIT-MM) | `CAUSADB_CHRONICLE.md` BIT-MM |
| PLAN_MULTI_MARKET pt C | (ver Chronicle BIT-MM) | `CAUSADB_CHRONICLE.md` BIT-MM |
| PLAN_SETUP_AND_CHRONICLE_LINKS F1 | BIT-S.1 (l.2073) `causadb setup` | `causadb/cli/_cmd_setup.py` |
| PLAN_SETUP_AND_CHRONICLE_LINKS F2 | BIT-S.1 (l.2092) Cross-Reference IDs | Diseño en Chronicle, implementación pendiente |
| PRODUCT_ROADMAP_COMMERCIAL | Absorbido por CAUSADB_STATE_AND_ROADMAP.md | `causadb/CAUSADB_STATE_AND_ROADMAP.md` |
| ROADMAP_AGENT_MEMORY_MIDDLEWARE | Absorbido por CAUSADB_STATE_AND_ROADMAP.md | `causadb/CAUSADB_STATE_AND_ROADMAP.md` |
