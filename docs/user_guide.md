# Guía de Usuario — CausaDB

## ¿Qué es CausaDB?
Una bitácora digital que registra todo lo que hacen las IAs en tu proyecto.
Como un "diario de a bordo" automático: sabés qué pasó, quién lo hizo, y podés volver atrás.

## Instalación
### Linux / macOS / Windows
```bash
pip install causadb
```

## Primeros pasos

### 1. Inicializar y Configurar
```bash
# Inicializar en el directorio actual
causadb init [--git-hooks] [--with-hook] [--telemetry-enabled] [--with-assistant]

# Setup completo (init + hooks + vigilante + daemon)
causadb setup /mi/proyecto
```

### 2. Servicios
Para levantar la API REST (no dashboard web) en `127.0.0.1:7457`:
```bash
causadb serve start
```

Para gestionar la vigilancia del proyecto:
```bash
causadb watch start --daemon
causadb watch status
causadb watch stop
```

## Actualizaciones
```bash
causadb update [--check]
```

## Telemetría y Privacidad
```bash
causadb telemetry status
causadb telemetry on
causadb telemetry off
```

## Comandos CLI

| Comando | Descripción |
|---------|-------------|
| `causadb init` | Inicializa CausaDB en el proyecto |
| `causadb setup` | Configuración completa (init + hooks + vigilancia) |
| `causadb log` | Agrega un evento al ledger |
| `causadb replay` | Reproduce eventos registrados |
| `causadb query` | Consulta el ledger de CausaDB |
| `causadb validate` | Verifica integridad del ledger |
| `causadb sentinel` | Ejecuta validaciones de seguridad |
| `causadb score` | Muestra métricas de productividad |
| `causadb why <file:line>` | Consulta origen de una línea |
| `causadb trace <file:line>` | Consulta trazabilidad de una línea |
| `causadb impact` | Analiza impacto de cambios |
| `causadb revive` | Recuperación de estado/comandos |
| `causadb resume` | Reanuda operaciones interrumpidas |
| `causadb ocb status` | Estado del OCB (Operational Context Buffer, memoria de corto plazo) |
| `causadb watch start` | Inicia el vigilante |
| `causadb watch stop` | Detiene el vigilante |
| `causadb watch status` | Estado del vigilante |
| `causadb telemetry` | Gestiona la telemetría |
| `causadb config set` | Configura parámetros (ej. keys) |
| `causadb update` | Actualiza la CLI |
| `causadb crash list` | Lista crashes registrados |

## Solución de problemas rápida

**"causadb: command not found"**
→ `pip install causadb` no completó o no está en el PATH.

**"No workspace found"**
→ No hay `.causadb/` en este directorio. Corré `causadb init`.

**La API REST no responde**
→ El daemon no está corriendo. Corré `causadb serve start`.

---

*Ultima actualizacion: 04/09/2026*
