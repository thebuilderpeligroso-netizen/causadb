# Experimento: recuperar el porqué sin re-descubrirlo

Comparación ciega entre reconstruir historia del proyecto por vía convencional
(Git + código + tests + docs + búsqueda normal) vs. consultando el registro
CausaDB. 4 preguntas reales, 12 corridas en chats nuevos y separados.

## Método (para poder repetirlo y romperlo)

- Cada corrida corre en un chat nuevo, sin contexto previo.
- Brazo A: prohibido CausaDB en todas sus formas (herramientas MCP, comando,
  carpeta `.causadb/`, crónica, guías). Solo repo.
- Brazo B: repo + CausaDB cuando aporte. Sin ver el informe del brazo A.
- Nada de web ni fuentes externas. Nada se modifica ni se commitea.
- Cada informe clasifica cada afirmación en HECHO (con cita consultada en la
  sesión), INFERENCIA o NO DETERMINABLE. Lo no verificado en sesión va como
  NO DETERMINABLE aunque se crea saber.
- Tiempos: reloj de la plataforma (hora del prompt → respuesta), mismo modelo
  en las 12 corridas (Muse Spark 1.3 Free, costo 0,00 en todas).
- Corrección ciega contra el registro después de cada par.
- Regla de honestidad: se publican todas las corridas, salgan como salgan.

## Ronda 1 — decisión fresca: cierre fail-closed sin llave API

| Métrica | A convencional | B registro | B con disciplina |
|---|---|---|---|
| Tokens totales | 75.556 | 84.347 | 43.954 |
| Evidencias / No determinables | 15 / 5 | 10 / 5 | s/d |
| Hallazgo | mecanismo completo con diffs | + 3 alternativas, números 14/113, códigos D-01..03, cadena de 5 BITs | mismos hallazgos, menos costo |

Lectura: con días de viejo, empate técnico con ventaja puntual del registro
(alternativas + seguimiento). Tiempos de esta ronda no medidos con reloj.

## Ronda 2 — 3 semanas: exponer MCP por HTTP de forma segura

| Métrica | A convencional | B registro | B con disciplina |
|---|---|---|---|
| Tiempo (reloj) | 16:00 min | 1:39 min | 1:51 min |
| Tokens totales | 95.544 | 90.613 | 42.548 |
| Evidencias / No determinables | 15 / 6 | 10 / 5 | 7 / 7 |
| Alternativas recuperadas | 0 de 4 | 4 de 4 (+3 extra) | 4 de 4 |
| Origen de la preocupación | no determinable | recuperado textual | recuperado |
| Investigación previa (WebMCP) | no determinable | recuperada | recuperada |

Lectura: el camino normal reconstruyó el mecanismo (8 commits, 6 diffs) pero
perdió toda la capa del porqué. El registro la trajo entera.

## Ronda 3 — propuesta viva: escritura remota compartida

| Métrica | A convencional | B registro | B con disciplina |
|---|---|---|---|
| Tiempo (reloj) | 1:49 min | 2:02 min | 1:22 min |
| Tokens totales | 53.459 | 63.638 | 40.232 |
| Veredicto | rechazar | rechazar | rechazar |
| Decisión previa citada | no | sí, textual + amenaza en 3 formas + alternativas | sí |

Lectura: mismo veredicto en los tres. La diferencia no fue velocidad sino
composición: el brazo con registro citó el acta previa en vez de re-derivar
los riesgos desde cero.

## Ronda 4 — trading fresco y bien documentado: indicadores Fase B-1

| Métrica | A convencional | B registro | B con disciplina |
|---|---|---|---|
| Tiempo (reloj) | 1:34 min | 3:02 min | s/d |
| Tokens totales | 64.780 | 127.362 | 53.959 |
| Quién/cuándo de la decisión | no determinable | recuperado | recuperado |
| Discusión que fijó números y topes | no determinable | recuperada | recuperada |
| 3 alternativas con motivos | no encontradas | recuperadas | recuperadas |

Lectura: con el porqué bien escrito en código/tests/docs, el camino
convencional fue el más rápido. Aun así no alcanzó autoría, deliberación de
auditoría ni números exactos del cierre.

## Disciplina de consulta (el hallazgo de costo)

Sin disciplina, el brazo con registro costó igual o más que excavar
(ronda 4: 127.362 vs 64.780) porque sumó todo: excavación completa MÁS
lecturas completas de crónica y registro. Con la disciplina de la escalera
(resumen primero, consultas puntuales tipo+tema en liviano, contenido completo
solo al evento decisivo, verificación en repo solo de lo señalado, parada a
tiempo), el costo quedó por debajo del convencional en las 4 rondas
(-42%, -55%, -25%, -17%).

## Lo que esto NO demuestra (límites honestos)

- No inventa lo nunca registrado: si una decisión no quedó en ningún lado,
  nadie la recupera (pregunta Postgres vs MySQL: respondida así desde el día 1).
- Con historia fresca y bien documentada, el repo compite bien e incluso gana
  en velocidad (ronda 4).
- Una corrida por brazo (direccional, no estadístico). Tiempos solo de reloj
  de plataforma. Todo medido en este proyecto.
- No se midió la cadena empresa completa (Jira + ADR + PR + CI): acá no existe.

## Textos usados (para replicar)

Pregunta sellada + reglas comunes: investigación de solo lectura, sin web,
sin modificar nada, sin preguntar (supuestos anotados), HECHO/INFERENCIA/NO
DETERMINABLE con cita de sesión, informe B1-B6 con mediciones y rastro.
Brazo A suma la prohibición total de CausaDB. Brazo B suma el registro cuando
aporte. Brazo con disciplina suma las 5 reglas: resumen primero, puntual +
liviano primero, prohibidas las búsquedas amplias y la reconstrucción
completa, el registro reemplaza la excavación, parar cuando el motivo responde.
