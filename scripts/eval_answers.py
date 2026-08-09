"""回答质量评估（LLM-as-judge）：对每个评估问题实际走一遍问答，
让评审 LLM 从六个维度打分（1-5）：

- 忠实度 faithfulness：回答是否严格基于引用来源，有没有编造（幻觉检测）
- 相关性 relevance：回答是否切中问题

用法（服务已启动、文档已入库、已用 gen_eval_set.py 生成评估集）：
  python scripts/eval_answers.py --user 用户名 --password 密码 --file eval_set.jsonl

输出各维度平均分与低分样本，供调 Prompt / 检索参数后对比。
"""
import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import OpenAI

from app.config import settings

JUDGE_PROMPT = """你是严格的问答质量评审。给定「问题 / 参考资料 / 回答」，从六个维度打 1-5 分：
- correctness（正确性）：结论是否正确；
- completeness（完整性）：是否覆盖问题要求；
- faithfulness（忠实度）：回答内容是否都能在参考资料中找到依据？编造扣分，明确说"没找到"不扣分。
- relevance（相关性）：回答是否直接切中问题？
- citation_correctness（引用正确性）：引用片段是否真正支持相邻结论；
- citation_completeness（引用完整性）：重要结论是否都有引用。
只输出 JSON：{"correctness":1-5,"completeness":1-5,"faithfulness":1-5,"relevance":1-5,"citation_correctness":1-5,"citation_completeness":1-5,"comment":"一句话点评"}"""


def ask(client: httpx.Client, headers: dict, question: str) -> tuple[str, list[dict]]:
    """走一遍问答接口，解析 SSE 拿到最终回答与来源。"""
    answer_parts: list[str] = []
    sources: list[dict] = []
    with client.stream("POST", "/api/chat", headers=headers, json={"question": question}) as resp:
        resp.raise_for_status()
        buffer = ""
        for text in resp.iter_text():
            buffer += text
            events = buffer.split("\n\n")
            buffer = events.pop()
            for raw in events:
                ev, dt = None, None
                for line in raw.splitlines():
                    if line.startswith("event: "):
                        ev = line[7:]
                    elif line.startswith("data: "):
                        dt = line[6:]
                if not ev or dt is None:
                    continue
                data = json.loads(dt)
                if ev == "sources":
                    sources = data
                elif ev == "delta":
                    answer_parts.append(data["text"])
    return "".join(answer_parts), sources


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--user", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--file", default="eval_set.jsonl")
    args = parser.parse_args()

    cases = [json.loads(x) for x in Path(args.file).read_text(encoding="utf-8").splitlines() if x.strip()]
    judge = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)

    dimensions = (
        "correctness",
        "completeness",
        "faithfulness",
        "relevance",
        "citation_correctness",
        "citation_completeness",
    )
    scores = {dimension: [] for dimension in dimensions}
    with httpx.Client(base_url=args.base_url, timeout=180) as client:
        login = client.post("/api/auth/login", json={"username": args.user, "password": args.password})
        login.raise_for_status()
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        for i, case in enumerate(cases, 1):
            question = case["question"]
            try:
                answer, sources = ask(client, headers, question)
                context = "\n\n".join(s["content"][:600] for s in sources) or "（无来源）"
                resp = judge.chat.completions.create(
                    model=settings.llm_model,
                    messages=[
                        {"role": "system", "content": JUDGE_PROMPT},
                        {"role": "user",
                         "content": f"问题：{question}\n\n参考资料：\n{context}\n\n回答：\n{answer[:2000]}"},
                    ],
                    temperature=0.0,
                    max_tokens=200,
                )
                text = resp.choices[0].message.content or ""
                verdict = json.loads(text[text.find("{"): text.rfind("}") + 1])
                values = {dimension: int(verdict[dimension]) for dimension in dimensions}
                for dimension, value in values.items():
                    scores[dimension].append(value)
                flag = " ⚠️" if min(values.values()) <= 3 else ""
                summary = " / ".join(f"{key} {value}" for key, value in values.items())
                print(f"[{i}/{len(cases)}] {summary}{flag}  {question}")
                if flag:
                    print(f"      点评: {verdict.get('comment', '')}")
            except Exception as exc:
                print(f"[{i}/{len(cases)}] 评估失败，跳过: {exc}")

    if scores["faithfulness"]:
        print(f"\n=== 共 {len(scores['faithfulness'])} 条 ===")
        for dimension in dimensions:
            print(f"{dimension}: {sum(scores[dimension]) / len(scores[dimension]):.2f} / 5")
    return 0


if __name__ == "__main__":
    sys.exit(main())
