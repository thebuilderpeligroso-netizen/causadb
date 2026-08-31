import os
from dataclasses import dataclass
from typing import Optional

@dataclass
class CausaDBConfig:
    ledger_path: str
    chronicle_path: Optional[str] = None
    redaction_enabled: bool = True
    hot_tier_size: int = 100
    ocb_base_path: Optional[str] = None
    ocb_threshold_events: int = 80
    ocb_partition_minutes: int = 15
    # F2 (M1) — default None: el cap por size + cantidad reemplaza al mtime.
    # Operadores que ya usaban retention_days pueden setearlo via env.
    ocb_retention_days: Optional[int] = None
    ocb_max_rewind_partitions: int = 5
    # F2 (M1) — LRU purge configurable (idea 5 operador):
    #   ocb_max_size_mb: tope por espacio en MB (0 = disabled).
    #   ocb_max_partitions: tope de seguridad por cantidad de particiones.
    # BIT-CHR.37 (Ola 2) — 20 -> 64: el backfill real de 85K eventos pesa
    # ~41MB (81% inline, no los 1-5MB estimados con refs $blob). 64MB
    # acomoda el bootstrap completo con margen. Contrato de producto:
    # el OCB es L1 volátil con costo FIJO acotado (nunca escala con el
    # ledger); lo purgado se recupera con `causadb query` o re-`rebuild`.
    ocb_max_size_mb: int = 64
    ocb_max_partitions: int = 500
    # F2 (M1) — fail_loud en tests para forzar visibilidad de degradación.
    ocb_fail_loud_on_degradation: bool = False
    blob_store_enabled: bool = True
    blob_store_path: Optional[str] = None
    # F.8.3 / OOM-fix — umbral (en bytes) sobre el payload serializado
    # a partir del cual LedgerWriter externaliza el payload al BlobStore
    # y lo reemplaza por ``{"$blob": content_hash}``. Payloads por debajo
    # del umbral se escriben inline en el ledger (sin blob huérfano).
    # Default 1024 (igual al default de BlobStore.threshold).
    blob_store_threshold: int = 1024
    dag_cache_min_events: int = 100
    # F.13.3.3 — Unified Score weights (section [score]).
    # score = 100 * (1 - w1*churn - w2*waste - w3*(1-survival))
    score_weight_churn: float = 0.3
    score_weight_waste: float = 0.3
    score_weight_survival: float = 0.4
    telemetry_enabled: bool = True  # #6 Privacidad Opt-out, default on
    # Fase 12 — StoryBoard persistente: dir base de los archivos de
    # detalle por sesión (<storyboard_path>/<tool>/<session_id>.json).
    storyboard_path: Optional[str] = None
    # F.12.1 — workspace root for auto-snapshots (writes events). When None,
    # the LedgerWriter's ``_maybe_auto_snapshot`` is a no-op.
    workspace_dir: Optional[str] = None

    def __post_init__(self):
        if not os.path.isabs(self.ledger_path):
            raise ValueError("ledger_path must be an absolute path")
        
        if self.chronicle_path is None:
            self.chronicle_path = os.path.join(os.path.dirname(self.ledger_path), "CAUSADB_CHRONICLE.md")
            
        if self.ocb_base_path is None:
            self.ocb_base_path = os.path.join(os.path.dirname(self.ledger_path), "ocb")

        if self.blob_store_path is None:
            self.blob_store_path = os.path.join(os.path.dirname(self.ledger_path), "blobs")

        if self.storyboard_path is None:
            self.storyboard_path = os.path.join(os.path.dirname(self.ledger_path), "stories")

    @classmethod
    def from_env(cls):
        return cls(
            ledger_path=os.getenv("CAUSADB_LEDGER_PATH", ""),
            chronicle_path=os.getenv("CAUSADB_CHRONICLE_PATH"),
            redaction_enabled=os.getenv("CAUSADB_REDACTION_ENABLED", "true").lower() == "true",
            hot_tier_size=int(os.getenv("CAUSADB_HOT_TIER_SIZE", "100")),
            ocb_threshold_events=int(os.getenv("CAUSADB_OCB_THRESHOLD_EVENTS", "80")),
            ocb_partition_minutes=int(os.getenv("CAUSADB_OCB_PARTITION_MINUTES", "15")),
            ocb_retention_days=(
                int(os.getenv("CAUSADB_OCB_RETENTION_DAYS", ""))
                if os.getenv("CAUSADB_OCB_RETENTION_DAYS", "").strip() else None
            ),
            ocb_max_rewind_partitions=int(os.getenv("CAUSADB_OCB_MAX_REWIND_PARTITIONS", "5")),
            ocb_max_size_mb=int(os.getenv("CAUSADB_OCB_MAX_SIZE_MB", "64")),
            ocb_max_partitions=int(os.getenv("CAUSADB_OCB_MAX_PARTITIONS", "500")),
            ocb_fail_loud_on_degradation=(
                os.getenv("CAUSADB_OCB_FAIL_LOUD_ON_DEGRADATION", "false").lower() == "true"
            ),
            blob_store_enabled=os.getenv("CAUSADB_BLOB_STORE_ENABLED", "true").lower() == "true",
            blob_store_path=os.getenv("CAUSADB_BLOB_STORE_PATH"),
            blob_store_threshold=int(os.getenv("CAUSADB_BLOB_STORE_THRESHOLD", "1024")),
            dag_cache_min_events=int(os.getenv("CAUSADB_DAG_CACHE_MIN_EVENTS", "100")),
            score_weight_churn=float(os.getenv("CAUSADB_SCORE_WEIGHT_CHURN", "0.3")),
            score_weight_waste=float(os.getenv("CAUSADB_SCORE_WEIGHT_WASTE", "0.3")),
            score_weight_survival=float(os.getenv("CAUSADB_SCORE_WEIGHT_SURVIVAL", "0.4")),
            telemetry_enabled=os.getenv("CAUSADB_TELEMETRY_ENABLED", "true").lower() == "true",
            storyboard_path=os.getenv("CAUSADB_STORYBOARD_PATH"),
            workspace_dir=os.getenv("CAUSADB_WORKSPACE_DIR"),
        )

    @classmethod
    def from_env_with_overrides(cls, ledger_path: Optional[str] = None) -> "CausaDBConfig":
        """Lee toda la config del entorno y optionally pisa ``ledger_path``.

        BIT-CHR.52 — usado por ``OCB.for_ledger(ledger_path=...)`` para que
        ``CAUSADB_OCB_THRESHOLD_EVENTS`` y los demás env vars tengan efecto
        en producción, mientras que el ``ledger_path`` pasado como
        argumento prevalece sobre el env var ``CAUSADB_LEDGER_PATH``.

        - ``ledger_path`` arg no None y no vacío → prevalece.
        - ``ledger_path`` arg None/vacío → usa el del env
          ``CAUSADB_LEDGER_PATH``.
        - Si ambos están vacíos → ``__post_init__`` levanta
          ``ValueError`` (no hay path absoluto válido).

        Implementación: NO delega en ``from_env()`` porque ese método hace
        ``cls(...)`` con ``ledger_path=os.getenv("CAUSADB_LEDGER_PATH", "")``
        → si el env var no está seteado, ``__post_init__`` valida
        "must be an absolute path" y levanta ANTES de que podamos pisar el
        campo. Por eso acá construimos directamente con el ``ledger_path``
        efectivo (argumento explícito o env) y los demás campos leídos
        individualmente del entorno (mismo orden que ``from_env``).
        """
        effective_ledger_path = (
            ledger_path if ledger_path
            else os.getenv("CAUSADB_LEDGER_PATH", "")
        )
        return cls(
            ledger_path=effective_ledger_path,
            chronicle_path=os.getenv("CAUSADB_CHRONICLE_PATH"),
            redaction_enabled=os.getenv("CAUSADB_REDACTION_ENABLED", "true").lower() == "true",
            hot_tier_size=int(os.getenv("CAUSADB_HOT_TIER_SIZE", "100")),
            ocb_threshold_events=int(os.getenv("CAUSADB_OCB_THRESHOLD_EVENTS", "80")),
            ocb_partition_minutes=int(os.getenv("CAUSADB_OCB_PARTITION_MINUTES", "15")),
            ocb_retention_days=(
                int(os.getenv("CAUSADB_OCB_RETENTION_DAYS", ""))
                if os.getenv("CAUSADB_OCB_RETENTION_DAYS", "").strip() else None
            ),
            ocb_max_rewind_partitions=int(os.getenv("CAUSADB_OCB_MAX_REWIND_PARTITIONS", "5")),
            ocb_max_size_mb=int(os.getenv("CAUSADB_OCB_MAX_SIZE_MB", "64")),
            ocb_max_partitions=int(os.getenv("CAUSADB_OCB_MAX_PARTITIONS", "500")),
            ocb_fail_loud_on_degradation=(
                os.getenv("CAUSADB_OCB_FAIL_LOUD_ON_DEGRADATION", "false").lower() == "true"
            ),
            blob_store_enabled=os.getenv("CAUSADB_BLOB_STORE_ENABLED", "true").lower() == "true",
            blob_store_path=os.getenv("CAUSADB_BLOB_STORE_PATH"),
            blob_store_threshold=int(os.getenv("CAUSADB_BLOB_STORE_THRESHOLD", "1024")),
            dag_cache_min_events=int(os.getenv("CAUSADB_DAG_CACHE_MIN_EVENTS", "100")),
            score_weight_churn=float(os.getenv("CAUSADB_SCORE_WEIGHT_CHURN", "0.3")),
            score_weight_waste=float(os.getenv("CAUSADB_SCORE_WEIGHT_WASTE", "0.3")),
            score_weight_survival=float(os.getenv("CAUSADB_SCORE_WEIGHT_SURVIVAL", "0.4")),
            telemetry_enabled=os.getenv("CAUSADB_TELEMETRY_ENABLED", "true").lower() == "true",
            storyboard_path=os.getenv("CAUSADB_STORYBOARD_PATH"),
            workspace_dir=os.getenv("CAUSADB_WORKSPACE_DIR"),
        )
