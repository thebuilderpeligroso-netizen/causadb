from typing import List, Dict, Optional


class CostRollup:
    @staticmethod
    def rollup_by_subtree(events: List[dict], prefix_depth: int = 2) -> Dict[str, float]:
        result = {}
        for ev in events:
            ctx = ev.get("ctx_id", "")
            cost = ev.get("cost", 0)
            parts = ctx.split("/")
            prefix = "/".join(parts[:prefix_depth])
            result[prefix] = result.get(prefix, 0) + cost
        return result

    @staticmethod
    def detect_discrepancy(proxy_cost: float, reported_cost: float, threshold: float = 0.10) -> bool:
        if proxy_cost == 0 and reported_cost == 0:
            return False
        max_cost = max(proxy_cost, reported_cost)
        if max_cost == 0:
            return False
        diff = abs(proxy_cost - reported_cost)
        return (diff / max_cost) > threshold

    @staticmethod
    def validate_hermes_consistency(events: List[dict]) -> Dict[str, dict]:
        """Auditoría pura (sin excepciones) de la consistencia de las 3 fuentes
        de tokens Hermes, agrupada por ``hermes_session_id`` (contrato H2.3):

        - ``API_ATTEMPT`` (per-request): suma ``tokens_out``.
        - ``COST_ACCOUNTED`` (agregado sesión): suma ``tokens_out``.
        - ``LLM_INVOKED`` (per-message): suma ``response_tokens``.

        Devuelve por sesión: ``{api_attempt_tokens_out,
        cost_accounted_tokens_out, llm_invoked_response_tokens,
        duplication_detected, discrepancy_detected}``.

        - ``duplication_detected``: ``llm_invoked_response_tokens`` > 0 y
          >= 2× ``cost_accounted_tokens_out`` (señal de que el agregado de
          sesión se repitió por mensaje assistant — el bug que H2.3 corrige).
          Requiere ``cost_accounted_tokens_out`` > 0: sin eventos de una
          fuente, el campo es 0 y no hay duplicación.
        - ``discrepancy_detected``: diferencia significativa (> umbral de
          ``detect_discrepancy``, ~10%) entre api y cost, SOLO cuando ambas
          fuentes tienen eventos (api > 0 y cost > 0). Si una fuente no trae
          eventos, no hay contra qué cruzar → no se declara discrepancia
          (omisión honesta, Art. V; evita falsos positivos en ledgers que
          no emiten COST_ACCOUNTED, p.ej. harvest Hermes sin OTel).
        - Eventos sin session id (o de otro tipo) se ignoran silenciosamente.
        """
        buckets: Dict[str, Dict[str, int]] = {}

        for ev in events:
            sid = None
            for key in ("hermes_session_id", "session_id", "__harvest_session_id"):
                val = ev.get(key)
                if isinstance(val, str) and val:
                    sid = val
                    break
            if not sid:
                continue
            etype = ev.get("type")
            if etype not in ("API_ATTEMPT", "COST_ACCOUNTED", "LLM_INVOKED"):
                continue

            bucket = buckets.setdefault(sid, {
                "api_attempt_tokens_out": 0,
                "cost_accounted_tokens_out": 0,
                "llm_invoked_response_tokens": 0,
            })
            value = ev.get("tokens_out" if etype != "LLM_INVOKED" else "response_tokens", 0)
            if not isinstance(value, (int, float)):
                value = 0
            if etype == "API_ATTEMPT":
                bucket["api_attempt_tokens_out"] += int(value)
            elif etype == "COST_ACCOUNTED":
                bucket["cost_accounted_tokens_out"] += int(value)
            else:  # LLM_INVOKED
                bucket["llm_invoked_response_tokens"] += int(value)

        result: Dict[str, dict] = {}
        for sid, b in buckets.items():
            api_out = b["api_attempt_tokens_out"]
            cost_out = b["cost_accounted_tokens_out"]
            llm_out = b["llm_invoked_response_tokens"]
            duplication = llm_out > 0 and cost_out > 0 and llm_out >= 2 * cost_out
            discrepancy = (
                api_out > 0
                and cost_out > 0
                and CostRollup.detect_discrepancy(float(api_out), float(cost_out))
            )
            result[sid] = {
                "api_attempt_tokens_out": api_out,
                "cost_accounted_tokens_out": cost_out,
                "llm_invoked_response_tokens": llm_out,
                "duplication_detected": duplication,
                "discrepancy_detected": discrepancy,
            }
        return result

    @staticmethod
    def total_cost(events: List[dict], ctx_id_prefix: Optional[str] = None) -> float:
        total = 0.0
        for ev in events:
            if ctx_id_prefix is None or ev.get("ctx_id", "").startswith(ctx_id_prefix):
                total += ev.get("cost", 0)
        return total

    @staticmethod
    def rollup_api_attempts(events: List[dict]) -> Dict[tuple, dict]:
        result = {}
        for ev in events:
            session_id = ev.get("hermes_session_id")
            model = ev.get("model")
            key = (session_id, model)
            if key not in result:
                result[key] = {"cost_usd": 0.0, "tokens_in": 0, "tokens_out": 0, "api_calls": 0}
            
            result[key]["cost_usd"] += ev.get("cost_usd", 0.0)
            result[key]["tokens_in"] += ev.get("tokens_in", 0)
            result[key]["tokens_out"] += ev.get("tokens_out", 0)
            result[key]["api_calls"] += 1
        return result
