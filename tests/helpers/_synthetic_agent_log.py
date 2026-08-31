"""Helper sintético para generar logs de Hermes."""
import os

def create_synthetic_agent_log(logs_dir: str, content: str) -> str:
    """Crea un archivo agent.log sintético en el directorio de logs."""
    os.makedirs(logs_dir, exist_ok=True)
    log_path = os.path.join(logs_dir, "agent.log")
    with open(log_path, "w") as f:
        f.write(content)
    return log_path
