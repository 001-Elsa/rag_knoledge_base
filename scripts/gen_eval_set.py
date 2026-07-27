"""自动生成检索评估集：从已入库的切片中随机采样，让 LLM 反向生成问题。

原理：切片内容是"标准答案所在处"，让 LLM 根据切片出题，
生成的 (问题, 关键词) 对天然带有 ground truth——检索系统若找不回这个切片就是漏召回。

用法（需要能连上数据库，Docker 部署时 5432 已映射到本机）：
  python scripts/gen_eval_set.py --samples 20 --out eval_set.jsonl
然后用 eval_retrieval.py 跑 Hit Rate / MRR，用 eval_answers.py 跑回答质量。
"""
import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from openai import OpenAI
from sqlalchemy import select

from app.config import settings
from app.db import SyncSessionLocal
from app.models import Chunk

GEN_PROMPT = """你在为检索系统构造评估数据。根据给出的文档片段，生成 1 个用户可能会问的自然问题，
答案必须能在这个片段里找到。同时给出 2-3 个"正确片段必然包含"的关键词（从片段原文中选取）。
只输出 JSON：{"question": "...", "expect_keywords": ["...", "..."]}"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=20, help="采样切片数（=生成问题数）")
    parser.add_argument("--out", default="eval_set.jsonl")
    args = parser.parse_args()

    with SyncSessionLocal() as db:
        chunks = list(db.execute(select(Chunk.content)).scalars())
    if not chunks:
        print("数据库中没有切片，请先上传文档")
        return 1
    samples = random.sample(chunks, min(args.samples, len(chunks)))

    client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
    lines = []
    for i, content in enumerate(samples, 1):
        try:
            resp = client.chat.completions.create(
                model=settings.llm_model,
                messages=[
                    {"role": "system", "content": GEN_PROMPT},
                    {"role": "user", "content": content[:1500]},
                ],
                temperature=0.5,
                max_tokens=200,
            )
            text = resp.choices[0].message.content or ""
            start, end = text.find("{"), text.rfind("}")
            item = json.loads(text[start : end + 1])
            assert item.get("question") and item.get("expect_keywords")
            lines.append(json.dumps(item, ensure_ascii=False))
            print(f"[{i}/{len(samples)}] {item['question']}")
        except Exception as exc:
            print(f"[{i}/{len(samples)}] 生成失败，跳过: {exc}")

    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n已生成 {len(lines)} 条评估数据 → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
