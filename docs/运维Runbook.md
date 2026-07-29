# 运维 Runbook

## 1. Outbox 堆积

告警：`outbox_pending_events > 100` 持续 5 分钟。

1. 检查 Redis Broker 健康与 Worker/Beat 日志；
2. 查询事件状态、重试次数和最后错误：

   ```sql
   SELECT id, event_type, aggregate_id, status, retry_count,
          next_retry_at, last_error
   FROM outbox_events
   WHERE status <> 'sent'
   ORDER BY created_at;
   ```

3. Broker 恢复后 Dispatcher 自动重试，不要手工删除事件；
4. `failed` 事件先确认根因，再把 `status` 改为 `pending`、`next_retry_at=now()`；
5. 对应 Document 若已 `ready`，重复投递会被 CAS 安全跳过。

## 2. 文档长期卡在处理中

查看 `status / stage / worker_id / heartbeat_at / processing_token`。对账任务每分钟扫描心跳超时记录，
转为 `retrying` 并创建恢复事件。若反复失败：

- parsing：检查文件魔数、页数、DOCX 解压大小、扫描 PDF；
- embedding：检查模型缓存、内存和外部模型服务；
- indexing：检查 pgvector、连接池、唯一约束和磁盘；
- 不要直接删除 active 版本；失败重建不会影响旧索引。

## 3. 对象不一致

先 dry-run：

```bash
celery -A app.tasks call reconcile_orphan_objects --args='[true]'
```

确认列出的 key 不在 `documents.object_key` 后再执行非 dry-run。任务默认只处理超过 24 小时安全窗口的
对象。数据库存在但对象缺失的 Document 应标记 failed 并从原始来源重新上传。

## 4. RLS 越权验证

生产发布前必须使用非 owner 的 `rag_app` 连接验证：

```sql
BEGIN;
SELECT set_config('app.user_id', '<user-a>', true);
SELECT id FROM knowledge_bases;
ROLLBACK;
```

切换为 user-b 后不能看到 user-a 未共享 Workspace 的行。禁止让 API 使用 `rag_admin` 或
`rag_worker` 连接串。

## 5. Refresh Token 重放

同一 Refresh Token 第二次使用会吊销整个 Token family，这是安全行为。用户需要重新登录。若重放量
异常，结合 audit、request_id、来源 IP 和反向代理日志排查 XSS、Cookie 泄漏或自动化攻击。

## 6. RAG 质量回退

1. 使用相同黄金集、模型版本和硬件重跑各 retrieval profile；
2. 检查失败 case 的 category，不只看总体平均；
3. 比较 chunk 配置、embedding_model、index_version；
4. 未通过 `check_quality_gate.py` 不切换线上 active 索引；
5. 无答案误答率回退的优先级高于一般 Hit Rate 提升。

## 7. 链路定位

从响应头 `X-Request-ID` 或审计表 `trace_id` 进入 Grafana/Tempo，依次检查：

```text
FastAPI → SQLAlchemy → Redis → Celery → parser → embedding → PostgreSQL → LLM
```

健康接口只给出依赖可用性，原始异常在受控日志与 Trace 中查看，不返回客户端。

## 8. 备份和恢复

- PostgreSQL：定期逻辑/物理备份并做恢复演练；
- MinIO/S3：启用版本控制或跨桶复制；
- Redis：不是业务事实来源，但 Broker 中的短期消息可配置 AOF；
- 恢复顺序：PostgreSQL → 对象存储 → Redis/Worker → API；
- 恢复后运行 Outbox、处理中任务和对象三类对账。

## 9. 死信队列与按阶段续跑

管理页：<http://localhost:8000/admin>（Owner/Admin）。

1. 入库重试耗尽或 Outbox 超限会写入 `dead_letter_tasks`；
2. 在管理页或 `POST /api/admin/dead-letters/{id}/replay` 人工重放；
3. `POST /api/admin/documents/{id}/resume` 可从 `parsing|chunking|embedding|indexing` 续跑；
4. Embedding 阶段按 batch 写 checkpoint 并刷新 `heartbeat_at`，长期卡住由 `reconcile_stale_ingestion` 回收租约。

## 10. 删除一致性

删除文档/知识库时：事务内标记 `deleting` + 写入 `resource.delete.requested` Outbox，由
`delete_resources` 可靠删除对象后再硬删行。对象暂时遗留时依赖 orphan 对账，不要手工乱删。

## 11. 审计哈希链

```bash
python scripts/verify_audit_chain.py
# 或对导出文件：
python scripts/verify_audit_chain.py --file audit-export.jsonl
```

导出：`GET /api/admin/audit-logs/export?workspace_id=...`。过期审计先归档到对象存储再 purge。

## 12. SLO 与错误预算

Prometheus 加载 `monitoring/slo-recording.yml`：

- `rag:api_success_ratio_5m` / `rag:availability_burn_rate_1h`
- `rag:availability_error_budget_remaining_30d`

告警：

- 1h 燃烧率 > 14x → page
- 30 天错误预算剩余 < 20% → warning

演练：临时制造 5xx（或断开依赖）观察告警是否在 Grafana/Alertmanager 触发，并在 Runbook 中记录时间线。
Trace 验证：对一次上传→入库→问答链路，用响应头 `X-Request-ID` / `trace_id` 在 Tempo 中确认
API→Outbox→Celery→Embedding→LLM 跨服务上下文连续。

## 13. 质量消融与 Agent 对比（需真实运行）

```bash
python scripts/run_ablation.py --user USER --password PASS
python scripts/eval_rag_vs_agent.py --user USER --password PASS
```

报告写入 `eval/reports/`。仓库不预填未经运行的 Hit Rate 提升百分比。
