# Solución de Problemas — CausaDB

## `ModuleNotFoundError: No module named 'mcp'`

**Causa:** Instalaste `causadb` sin las dependencias opcionales del MCP server.

**Solución:**
```bash
pip install causadb[dev]
```

## `externally-managed-environment`

**Causa:** Tu sistema operativo (Debian/Ubuntu/Fedora moderno) bloquea `pip install` fuera de un virtual environment.

**Solución:**
```bash
python -m venv .venv
source .venv/bin/activate
pip install causadb
```

## `NoWorkspaceError`

**Causa:** No hay un workspace de CausaDB en el directorio actual.

**Solución:**
```bash
causadb init
```
Esto crea `.causadb/` con el ledger y los archivos de configuración.

## Crash al iniciar el daemon

**Causa:** El daemon anterior no se cerró correctamente y dejó PID files stale.

**Solución:**
```bash
causadb crash list       # ver crashes registrados
causadb crash delete     # limpiar crashes
causadb serve start      # reintentar
```

## El servicio REST API no carga

**Causa:** El daemon no está corriendo o el puerto está ocupado.

**Solución:**
```bash
# Verificar si el servicio corre
causadb watch status

# Iniciarlo si no corre
causadb serve start

# Verificar puerto
curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:7457
```

## La telemetría no se desactiva

**Causa:** El cambio se hizo pero no se persistió.

**Solución:**
```bash
causadb telemetry off
causadb telemetry status   # debe decir "off"
```

## `Keyring` no disponible

**Causa:** El módulo `keyring` de Python no está instalado o no hay un backend disponible en el sistema.

**Solución:**
```bash
pip install keyring
# o usá variables de entorno como fallback
export CAUSADB_OPENAI_KEY="sk-..."
export CAUSADB_ANTHROPIC_KEY="sk-ant-..."
```

## `Permission denied` al leer el ledger

**Causa:** El archivo `.causadb/ledger.log` tiene permisos incorrectos.

**Solución:**
```bash
chmod -R u+r .causadb/
chmod u+w .causadb/ledger.log
```

## `causadb: command not found`

**Causa:** No instalaste CausaDB o no está en el PATH.

**Solución:**
```bash
pip install causadb
```

## El proxy no captura tráfico LLM

**Causa:** El proxy no está corriendo o la configuración del cliente apunta a otro puerto.

**Solución:**
```bash
causadb proxy-server start
# Configurá tu cliente (OpenAI, Ollama) para usar el endpoint del proxy:
# OpenAI: http://127.0.0.1:4242/openai/v1
# Anthropic: http://127.0.0.1:4242/anthropic/v1
```

## Error de hash chain al validar

**Causa:** El ledger fue modificado externamente (editor de texto, script manual).

**Solución:**
```bash
causadb validate   # muestra en qué posición falla
```
Si es un error legítimo, restaurá desde backup. Para resetear el workspace, crea uno nuevo en otro directorio.

## `watch` no detecta cambios de archivos

**Causa:** Falta `watchdog` (dependencia opcional).

**Solución:**
```bash
pip install watchdog
causadb watch start --daemon
```
