# Preguntas Frecuentes — CausaDB

## ¿Qué es un ledger?
Es un archivo append-only donde se registran todos los eventos en orden. Cada evento tiene un hash que apunta al anterior, formando una cadena. Si alguien modifica algo viejo, el hash deja de coincidir y CausaDB lo detecta al instante.

## ¿CausaDB guarda mis datos? (privacidad)
CausaDB almacena todo localmente en tu máquina. La única telemetría opcional que se envía son métricas anónimas de uso (versión, cantidad de eventos, sin tus archivos ni prompts). Podés desactivarla en cualquier momento con `causadb telemetry off`.

## ¿Puedo usar CausaDB sin internet?
Sí. Todo corre local. La telemetría se desactiva y el servicio funciona en localhost. No necesitás internet para nada.

## ¿Cómo comparto mi proyecto con otro dev?
Compartí la carpeta `.causadb/` junto con el código (está en tu VCS). El otro dev corre `causadb replay` y ve el estado completo. El ledger es portátil entre máquinas.

## ¿Qué pasa si se borra el ledger?
Si se borra `.causadb/ledger.log` perdés el histórico. Pero si tenés backup o el proyecto está en git, podés restaurarlo. Recomendación: agregá `.causadb/` a tu `.gitignore` **excepto** el ledger, o usá `causadb export` para hacer backups periódicos.

## ¿CausaDB es gratis?
Sí. CausaDB cuenta actualmente con un modelo de acceso gratuito funcional para uso individual.
*Nota: Los planes de precios (Pro/Enterprise) y la disponibilidad del binario standalone se encuentran actualmente en preparación para la release pública.*

## Importación y Exportación de datos
Usá `causadb import` y `causadb export` para transferir datos, soportando formato OTel:

```bash
# Importar desde OTel
causadb import --format otel --file <ruta_al_archivo>

# Exportar a OTel
causadb export --format otel --endpoint <url_del_endpoint>
```

## Errores comunes

### "No module named 'mcp'"
Te falta instalar las dependencias opcionales:
```bash
pip install causadb[dev]
```

### "externally-managed-environment"
Usá un virtual environment:
```bash
python -m venv .venv
source .venv/bin/activate
pip install causadb
```

### "NoWorkspaceError"
No hay un workspace inicializado. Corré `causadb init` en el proyecto. El CLI tiene auto-descubrimiento y detectará automáticamente el archivo `.causadb/ledger.log` en el directorio.

## ¿CausaDB funciona con cualquier modelo de IA?
Sí. Funciona con modelos locales (Ollama, LM Studio) y en la nube (OpenAI, Anthropic, Google). El proxy captura el tráfico LLM automáticamente.

## ¿Necesito saber programar para usarlo?
No. Los comandos básicos son simples. Para funciones avanzadas (consultas personalizadas, integraciones), sí conviene tener nociones de terminal.

## ¿Cuánto espacio ocupa el ledger?
Muy poco. Un evento típico son ~500 bytes. Una sesión intensiva de 8 horas genera ~1-2 MB. Está diseñado para ser liviano.

## ¿CausaDB tiene API?
Sí. Expone un **MCP server** (21 tools + 4 recursos), una **API REST** en `http://127.0.0.1:7457`, y puede exponer el MCP por **HTTP (streamable-http)** para agentes remotos, con subconjunto de lectura seguro y bind-safety (sin API key solo escucha en tu máquina).

## ¿Qué es el Score?
Un número del 0 al 100 que mide eficiencia de la sesión. Combina:
- Churn (líneas escritas y borradas)
- Waste (costo de LLM en código revertido)
- Survival (código que queda en el estado final)

## ¿Cómo reinicio el ledger desde cero?
Simplemente crea un nuevo directorio, entra en él y corre `causadb init`. No existe un comando para borrar/prunear el ledger actual.

## ¿CausaDB es open source?
Sí. El código está en [github.com/causadb/causadb](https://github.com/causadb/causadb). Licencia MIT.

---

*Ultima actualizacion: 04/09/2026*
