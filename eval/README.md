# 固定 RAG 评估集

`golden_set.jsonl` 是人工编写并可代码审查的最小回归集，配套文档位于 `fixtures/`。它覆盖事实、
同义改写、编号、跨段对比、权限、无答案和间接 Prompt Injection。项目不得把 LLM 随机生成的问题
直接并入该文件；新增样本需要人工核对参考答案与预期证据。

当前仓库不预填“漂亮指标”。启动完整服务、上传两个夹具后，用 `scripts/eval_retrieval.py` 生成结果，
再将报告保存到 `eval/reports/`。主分支基线确认后，可用：

```bash
for profile in vector hybrid hybrid_rerank parent_child multi_query; do
  python scripts/eval_retrieval.py --user USER --password PASS \
    --profile "$profile" --out "eval/reports/$profile.json"
done
```

```bash
python scripts/check_quality_gate.py \
  --baseline eval/baseline.json \
  --current eval/reports/current.json
```

门禁默认约束：Hit Rate@5 相对下降不超过 2 个百分点、MRR 不下降超过 0.02、无答案误答率不恶化、
P95 检索延迟不回退超过 20%。没有实际报告时 CI 不声明质量提升。
