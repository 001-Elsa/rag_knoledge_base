# 固定 RAG 评估集

`golden_set.jsonl` 是人工编写并可代码审查的最小回归集，配套文档位于 `fixtures/`。它覆盖事实、
同义改写、编号、跨段对比、权限、无答案和间接 Prompt Injection。项目不得把 LLM 随机生成的问题
直接并入该文件；新增样本需要人工核对参考答案与预期证据。

当前仓库不编造“漂亮指标”。`eval/reports/ablation_summary.md` 保留了一次带模型与时间信息的本地实测
快照；重新运行脚本会覆盖它。若要通过已启动的完整服务评估，先上传两个夹具后执行：

```bash
# 五档检索消融（写入 eval/reports/*.json + ablation_summary.md）
python scripts/run_ablation.py --user USER --password PASS

# RAG vs Agent 对比
python scripts/eval_rag_vs_agent.py --user USER --password PASS
```

或按 profile 单独跑：

```bash
for profile in vector hybrid hybrid_rerank parent_child multi_query; do
  python scripts/eval_retrieval.py --user USER --password PASS \
    --profile "$profile" --out "eval/reports/$profile.json"
done
```

```bash
python scripts/check_quality_gate.py \
  --baseline eval/baseline.json \
  --current eval/reports/hybrid.json

python scripts/check_injection_gate.py
```

不启动 API、直接对真实 PostgreSQL 和本地 Embedding 做消融时，可使用：

```bash
# DATABASE_URL 必须指向已迁移且有写权限的 PostgreSQL；使用 Compose 时可叠加
# docker-compose.eval.yml 暴露本机端口。
python scripts/run_local_ablation.py
# 加上 multi_query（需要有效 LLM_API_KEY）：
python scripts/run_local_ablation.py --with-llm
```

门禁默认约束：Hit Rate@5 相对下降不超过 2 个百分点、MRR 不下降超过 0.02、无答案误答率不恶化、
P95 检索延迟不回退超过 20%。CI 额外用确定性伪向量跑离线 hybrid 门禁（`scripts/ci_eval_retrieval.py`），
不等于线上真实 Embedding/LLM 质量报告。

## 实测消融结果

以下是 2026-07-29 的一次本地快照，由 `scripts/run_local_ablation.py` 在
BAAI/bge-small-zh-v1.5 嵌入模型下实测得到，不是持续更新的生产基准。
Golden set: 8 个可回答 + 2 个不可回答（共 10 条）。

| Strategy | Hit Rate@5 | MRR | nDCG@5 | No-answer FP | P95 ms |
|---|---:|---:|---:|---:|---:|
| vector | 1.000 | 0.938 | 0.954 | 1.000 | 137.8 |
| hybrid | 1.000 | 0.938 | 0.954 | 1.000 | 164.5 |
| hybrid_rerank | 1.000 | 1.000 | 1.000 | 1.000 | 473386.9 |
| parent_child | 1.000 | 0.938 | 0.954 | 1.000 | 417.6 |
| multi_query | 1.000 | 0.938 | 0.954 | 1.000 | 1921.7 |

**已知限制：**
- No-answer 误答率 100%：当前仅有 2 条不可回答样本，证据门控（`assess_evidence`）主要依赖
  Chunk 数量和融合分；在这次数据上检索到候选后均判定为可回答。需要在黄金集中增加更多
  不可回答样本（如覆盖不同主题、不同领域），并结合语义相似度阈值改进门控。
- Reranker 延迟 ~473s：首次加载 BAAI/bge-reranker-base 模型（~1GB），
  后续推理约为 100-200ms/query。报告中的 P95 含首次冷启动。
- 所有策略 Hit Rate@5 = 1.0：当前 8 条可回答样本覆盖范围有限，
  策略间的区分度需要扩增黄金集（建议扩大到 50+ 条）才能显现。

**如何复现：**
```bash
python scripts/run_local_ablation.py --with-llm
```

**如何对比 RAG vs Agent：**
```bash
python scripts/eval_rag_vs_agent.py --user USER --password PASS
```
结果写入 `eval/reports/rag_vs_agent.json`。
