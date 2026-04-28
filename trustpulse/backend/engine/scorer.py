from typing import List, Optional, Tuple
from .rules import RuleResult, evaluate_all_rules


def risk_level(score: float) -> str:
    if score <= 25:
        return "LOW"
    if score <= 50:
        return "MEDIUM"
    if score <= 75:
        return "HIGH"
    return "CRITICAL"


def compute_risk_score(
    event: dict,
    baseline: Optional[dict],
    context: dict,
) -> Tuple[float, str, List[dict]]:
    results = evaluate_all_rules(event, baseline, context)
    total   = min(sum(r.score_contribution for r in results if r.fired), 100.0)
    level   = risk_level(total)

    fired_rules = [
        {
            "rule_id":            r.rule_id,
            "rule_name":          r.rule_name,
            "fired":              r.fired,
            "description":        r.description,
            "score_contribution": r.score_contribution,
            "hipaa_ref":          r.hipaa_ref,
            "severity":           r.severity,
            "confidence":         r.confidence,
            "supporting_fields":  r.supporting_fields,
            "limitations":        r.limitations,
            "not_evaluated":      r.not_evaluated,
            "not_evaluated_reason": r.not_evaluated_reason,
        }
        for r in results if r.fired or r.not_evaluated
    ]
    return total, level, fired_rules
