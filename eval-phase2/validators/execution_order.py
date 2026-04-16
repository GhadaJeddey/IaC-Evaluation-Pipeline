"""
Checks execution order correctness for multi-instance bundle scenarios (Tier B).
Rule: dependents must be acted on before their primaries.
For single-instance scenarios, always returns 1.0.
"""
from dataclasses import dataclass
from typing import Any

@dataclass
class ValidatorResult:
    name:    str
    passed:  bool
    score:   float
    details: str


_ROLE_ORDER = {
    # Lower = must come first
    "backup":             0,
    "dependent_secondary": 1,
    "dependent_primary":  2,
    "bursty":             3,
    "steady":             3,
}


def validate(scenario: dict, llm_response: dict) -> ValidatorResult:
    """
    For bundle scenarios: verify that the LLM's action sequence (if specified)
    respects dependency order — secondaries/backups before primaries.
    """
    resources = scenario.get("flagged_resources", [])

    if len(resources) <= 1:
        return ValidatorResult("execution_order", True, 1.0, "Single resource — order N/A")

    # LLM emits "execution_order": ["instance_id_1", "instance_id_2", ...]
    actual_ids = llm_response.get("execution_order", [])
    if not actual_ids:
        return ValidatorResult(
            "execution_order", False, 0.5,
            "LLM did not populate execution_order for multi-instance scenario"
        )

    # Build expected order: sort actionable resources by role priority
    active_resources = [
        r for r in resources
        if r["agent2_decision"]["action"] not in ("CLEAN", "NEEDS_REVIEW", "SKIP")
    ]
    expected_order = sorted(
        active_resources,
        key=lambda r: _ROLE_ORDER.get(r["role"], 3)
    )
    expected_ids = [r["instance_id"] for r in expected_order]

    # Check that any dependent roles appear before primaries
    violations = []
    for i, iid in enumerate(actual_ids):
        res = next((r for r in active_resources if r["instance_id"] == iid), None)
        if not res:
            continue
        role_rank = _ROLE_ORDER.get(res["role"], 3)
        for j, prev_iid in enumerate(actual_ids[:i]):
            prev_res = next((r for r in active_resources if r["instance_id"] == prev_iid), None)
            if prev_res and _ROLE_ORDER.get(prev_res["role"], 3) > role_rank:
                violations.append(f"{prev_iid}({prev_res['role']}) before {iid}({res['role']})")

    if violations:
        return ValidatorResult(
            "execution_order", False, 0.0,
            f"Order violations: {violations}"
        )

    return ValidatorResult("execution_order", True, 1.0, f"Correct order: {actual_ids}")
