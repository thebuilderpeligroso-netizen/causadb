from dataclasses import dataclass
from typing import List
from causadb._drift_detector import check_hash_chain, check_replay_consistency, check_causal_drift

@dataclass
class RuleResult:
    rule_name: str
    passed: bool

@dataclass
class SentinelReport:
    all_rules_pass: bool
    summary: str
    results: List[RuleResult]

def evaluate_rules(ledger_path: str) -> SentinelReport:
    results = []
    
    # Regla 1
    hc_report = check_hash_chain(ledger_path)
    results.append(RuleResult("hash_chain_integrity", hc_report.is_valid))
    
    # Regla 2
    rc_report = check_replay_consistency(ledger_path)
    results.append(RuleResult("replay_consistency", rc_report.is_valid))
    
    # Regla 3
    cd_report = check_causal_drift(ledger_path)
    results.append(RuleResult("causal_integrity", cd_report.is_valid))
    
    all_pass = all(r.passed for r in results)
    return SentinelReport(
        all_rules_pass=all_pass,
        summary="OK" if all_pass else "DRIFT_DETECTED",
        results=results
    )
