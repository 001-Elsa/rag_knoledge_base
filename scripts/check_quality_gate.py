"""Compare a measured RAG report with the accepted main-branch baseline."""

import argparse
import json
from pathlib import Path


def check_quality(
    baseline: dict,
    current: dict,
    *,
    max_hit_rate_drop: float = 0.02,
    max_mrr_drop: float = 0.02,
    max_latency_regression: float = 0.20,
) -> list[str]:
    failures = []
    if current["hit_rate_at_5"] < baseline["hit_rate_at_5"] - max_hit_rate_drop:
        failures.append("Hit Rate@5 regression exceeds 2 percentage points")
    if current["mrr"] < baseline["mrr"] - max_mrr_drop:
        failures.append("MRR regression exceeds 0.02")
    if (
        current["no_answer_false_positive_rate"]
        > baseline["no_answer_false_positive_rate"]
    ):
        failures.append("no-answer false-positive rate regressed")
    latency_limit = baseline["p95_retrieval_ms"] * (1 + max_latency_regression)
    if current["p95_retrieval_ms"] > latency_limit:
        failures.append("P95 retrieval latency regressed by more than 20%")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--current", required=True)
    args = parser.parse_args()
    baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    current = json.loads(Path(args.current).read_text(encoding="utf-8"))
    failures = check_quality(baseline, current)
    if failures:
        print("\n".join(f"[FAIL] {failure}" for failure in failures))
        return 1
    print("[PASS] RAG quality and latency gates satisfied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
