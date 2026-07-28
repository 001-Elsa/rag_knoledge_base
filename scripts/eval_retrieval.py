"""Run the reviewed golden set and emit machine-readable retrieval metrics."""

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

import httpx


def _relevant(source: dict, case: dict) -> bool:
    expected_documents = set(case.get("expected_documents", []))
    document_match = (
        not expected_documents or source.get("filename") in expected_documents
    )
    keywords = case.get("expected_keywords", [])
    keyword_match = all(keyword in source.get("content", "") for keyword in keywords)
    return document_match and keyword_match


def _first_correct_rank(sources: list[dict], case: dict) -> int | None:
    for rank, source in enumerate(sources, start=1):
        if _relevant(source, case):
            return rank
    return None


def _ndcg(sources: list[dict], case: dict, k: int = 5) -> float:
    gains = [1.0 if _relevant(source, case) else 0.0 for source in sources[:k]]
    dcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains, 1))
    return dcg  # one expected evidence group means ideal DCG is 1


def _retrieve(
    client: httpx.Client, headers: dict, question: str, profile: str
) -> tuple[list, dict, float]:
    started = time.perf_counter()
    sources = []
    evidence = {}
    current_event = ""
    with client.stream(
        "POST",
        "/api/chat",
        headers=headers,
        json={"question": question, "retrieval_profile": profile},
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("event: "):
                current_event = line[7:]
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
                if current_event == "sources":
                    sources = payload
                elif current_event == "evidence":
                    evidence = payload
                    break
    return sources, evidence, (time.perf_counter() - started) * 1000


def evaluate(
    cases: list[dict], client: httpx.Client, headers: dict, profile: str
) -> dict:
    hits = 0
    reciprocal_ranks = []
    ndcg_values = []
    false_positive_no_answers = 0
    no_answer_cases = 0
    latencies = []
    failures = []

    for case in cases:
        sources, evidence, latency_ms = _retrieve(
            client, headers, case["question"], profile
        )
        latencies.append(latency_ms)
        if case["answerable"]:
            rank = _first_correct_rank(sources, case)
            hits += int(rank is not None)
            reciprocal_ranks.append(1.0 / rank if rank else 0.0)
            ndcg_values.append(_ndcg(sources, case))
            if rank is None:
                failures.append({"id": case["id"], "reason": "retrieval_miss"})
        else:
            no_answer_cases += 1
            false_positive = evidence.get("answerable", bool(sources))
            false_positive_no_answers += int(false_positive)
            if false_positive:
                failures.append({"id": case["id"], "reason": "false_positive_answer"})

    answerable_count = max(1, sum(case["answerable"] for case in cases))
    sorted_latency = sorted(latencies)
    p95_index = min(len(sorted_latency) - 1, math.ceil(len(sorted_latency) * 0.95) - 1)
    return {
        "case_count": len(cases),
        "retrieval_profile": profile,
        "hit_rate_at_5": hits / answerable_count,
        "mrr": statistics.fmean(reciprocal_ranks) if reciprocal_ranks else 0.0,
        "ndcg_at_5": statistics.fmean(ndcg_values) if ndcg_values else 0.0,
        "no_answer_false_positive_rate": (
            false_positive_no_answers / no_answer_cases if no_answer_cases else 0.0
        ),
        "p95_retrieval_ms": sorted_latency[p95_index] if sorted_latency else 0.0,
        "failures": failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--file", default="eval/golden_set.jsonl")
    parser.add_argument("--out", default="eval/reports/current.json")
    parser.add_argument(
        "--profile",
        default="hybrid",
        choices=["vector", "hybrid", "hybrid_rerank", "parent_child", "multi_query"],
    )
    args = parser.parse_args()
    cases = [
        json.loads(line)
        for line in Path(args.file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not cases:
        print("评估集为空")
        return 1

    with httpx.Client(base_url=args.base_url, timeout=120) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": args.user, "password": args.password},
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        report = evaluate(cases, client, headers, args.profile)

    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"报告已写入 {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
