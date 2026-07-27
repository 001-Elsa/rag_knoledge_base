"""检索质量评估脚本：计算 Hit Rate@K 和 MRR。

用法：
  1. 准备评估集 eval_set.jsonl，每行一条：
     {"question": "退款要几天到账", "expect_keywords": ["退款", "3-5个工作日"]}
     expect_keywords 表示正确答案所在片段应包含的关键词（全部命中才算该片段正确）。
  2. 确保服务已启动、文档已入库，然后运行：
     python scripts/eval_retrieval.py --user 用户名 --password 密码 --file eval_set.jsonl

指标含义：
  - Hit Rate@K：K 个检索结果中至少有一个正确片段的问题占比（衡量"找没找到"）
  - MRR：第一个正确片段排名的倒数的平均值（衡量"排得靠不靠前"）
"""
import argparse
import json
import sys
from pathlib import Path

import httpx


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--file", default="scripts/eval_set.jsonl")
    args = parser.parse_args()

    cases = [json.loads(line) for line in Path(args.file).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not cases:
        print("评估集为空")
        return 1

    with httpx.Client(base_url=args.base_url, timeout=120) as client:
        login = client.post("/api/auth/login", json={"username": args.user, "password": args.password})
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        hits = 0
        reciprocal_ranks: list[float] = []
        for case in cases:
            sources = _ask(client, headers, case["question"])
            rank = _first_correct_rank(sources, case["expect_keywords"])
            if rank is not None:
                hits += 1
                reciprocal_ranks.append(1.0 / rank)
            else:
                reciprocal_ranks.append(0.0)
                print(f"  [MISS] {case['question']}")

    k = "K"
    print(f"\n评估完成（{len(cases)} 个问题）")
    print(f"Hit Rate@{k}: {hits / len(cases):.2%}")
    print(f"MRR:        {sum(reciprocal_ranks) / len(cases):.4f}")
    return 0


def _ask(client: httpx.Client, headers: dict, question: str) -> list[dict]:
    """调用问答接口，只解析 sources 事件（不需要等 LLM 答案生成完）。"""
    with client.stream("POST", "/api/chat", headers=headers, json={"question": question}) as resp:
        resp.raise_for_status()
        buffer = ""
        for text in resp.iter_text():
            buffer += text
            for raw in buffer.split("\n\n"):
                if "event: sources" in raw:
                    for line in raw.splitlines():
                        if line.startswith("data: "):
                            return json.loads(line[6:])
    return []


def _first_correct_rank(sources: list[dict], expect_keywords: list[str]) -> int | None:
    for i, source in enumerate(sources, start=1):
        if all(kw in source["content"] for kw in expect_keywords):
            return i
    return None


if __name__ == "__main__":
    sys.exit(main())
