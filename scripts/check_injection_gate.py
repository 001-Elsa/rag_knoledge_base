"""Prompt-injection release gate (item 15).

Fails CI when the golden set / malicious fixture no longer exercises injection
patterns, or when high-risk detection regresses on the known sample.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.evidence import RISK_HIGH, injection_risk


def main() -> int:
    golden = Path("eval/golden_set.jsonl")
    rows = [
        json.loads(line)
        for line in golden.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    injection_cases = [row for row in rows if row.get("category") == "prompt_injection"]
    if not injection_cases:
        print("[FAIL] golden set missing prompt_injection category")
        return 1

    malicious = Path("eval/fixtures/malicious_document.md").read_text(encoding="utf-8")
    if injection_risk(malicious) != RISK_HIGH:
        print("[FAIL] malicious fixture no longer classified as high risk")
        return 1

    # Benign policy fixture must stay clean so we do not quarantine legitimate docs.
    policy = Path("eval/fixtures/company_policy.md").read_text(encoding="utf-8")
    if injection_risk(policy) == RISK_HIGH:
        print("[FAIL] company_policy.md incorrectly classified as high risk")
        return 1

    print(
        f"[PASS] injection gate: {len(injection_cases)} golden cases, "
        "malicious=high, policy=clean"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
