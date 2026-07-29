"""Compare fixed RAG vs Agent on the same golden set (item 23).

Reports accuracy (keyword/reference hit), average LLM round-trips (Agent tool
steps + final answer), token cost, and latency. Requires a running API with LLM
credentials and ingested fixtures.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import httpx


def _consume_sse(
    client: httpx.Client, headers: dict, question: str, mode: str
) -> dict:
    answer_parts: list[str] = []
    sources: list[dict] = []
    usage = {}
    tool_calls = 0
    tool_failures = 0
    no_answer = False
    current_event = ""
    started = time.perf_counter()
    with client.stream(
        "POST",
        "/api/chat",
        headers=headers,
        json={"question": question, "mode": mode, "retrieval_profile": "hybrid"},
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("event: "):
                current_event = line[7:]
            elif line.startswith("data: "):
                payload = json.loads(line[6:])
                if current_event == "sources":
                    sources = payload
                elif current_event == "delta":
                    answer_parts.append(payload.get("text", ""))
                elif current_event == "done":
                    usage = payload.get("usage") or {}
                    no_answer = bool(payload.get("no_answer"))
                elif current_event == "tool_call":
                    tool_calls += 1
                elif current_event == "tool_result":
                    summary = str(payload.get("summary") or "")
                    if summary.startswith("工具执行出错") or summary.startswith("错误"):
                        tool_failures += 1
                elif current_event == "error":
                    tool_failures += 1
    return {
        "answer": "".join(answer_parts),
        "sources": sources,
        "usage": usage,
        "tool_calls": tool_calls,
        "tool_failures": tool_failures,
        "no_answer": no_answer,
        "latency_ms": (time.perf_counter() - started) * 1000,
    }


def _correct(case: dict, result: dict) -> bool:
    if not case.get("answerable", True):
        return result["no_answer"] or "没有找到" in result["answer"]
    keywords = case.get("expected_keywords") or []
    if keywords:
        return all(kw in result["answer"] or any(kw in s.get("content", "") for s in result["sources"]) for kw in keywords)
    reference = case.get("reference_answer") or ""
    return bool(reference) and reference[:20] in result["answer"]


def evaluate(cases: list[dict], client: httpx.Client, headers: dict, mode: str) -> dict:
    correct = 0
    latencies = []
    prompt_tokens = []
    completion_tokens = []
    tool_calls = []
    tool_failures = 0
    for case in cases:
        result = _consume_sse(client, headers, case["question"], mode)
        correct += int(_correct(case, result))
        latencies.append(result["latency_ms"])
        prompt_tokens.append(result["usage"].get("prompt_tokens", 0))
        completion_tokens.append(result["usage"].get("completion_tokens", 0))
        tool_calls.append(result["tool_calls"])
        tool_failures += result["tool_failures"]
    n = max(1, len(cases))
    return {
        "mode": mode,
        "case_count": len(cases),
        "accuracy": correct / n,
        "avg_latency_ms": statistics.fmean(latencies) if latencies else 0.0,
        "avg_prompt_tokens": statistics.fmean(prompt_tokens) if prompt_tokens else 0.0,
        "avg_completion_tokens": statistics.fmean(completion_tokens) if completion_tokens else 0.0,
        "avg_tool_calls": statistics.fmean(tool_calls) if tool_calls else 0.0,
        "tool_failure_rate": tool_failures / max(1, sum(tool_calls) or 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--file", default="eval/golden_set.jsonl")
    parser.add_argument("--out", default="eval/reports/rag_vs_agent.json")
    args = parser.parse_args()

    cases = [
        json.loads(line)
        for line in Path(args.file).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with httpx.Client(base_url=args.base_url, timeout=300) as client:
        login = client.post(
            "/api/auth/login",
            json={"username": args.user, "password": args.password},
        )
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        rag = evaluate(cases, client, headers, "rag")
        agent = evaluate(cases, client, headers, "agent")

    report = {"rag": rag, "agent": agent}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
