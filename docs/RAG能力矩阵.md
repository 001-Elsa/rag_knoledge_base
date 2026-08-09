# RAG 能力落实矩阵

本表对应项目的可运行主链路。列表里互为替代方案的产品不会同时堆叠：向量存储选择
PostgreSQL + pgvector，异步队列选择 Celery + Redis，在线服务选择 FastAPI + SSE。
这能避免“双写一致性、部署体积和运维面”无收益地扩大；接口、配置、迁移和测试均围绕这条主链路。

## 1. 数据处理

| 要点 | 落实位置与行为 |
|---|---|
| PDF、Word、Excel、CSV、HTML、Markdown、文本、图片/扫描件 | `services/parser.py`；上传白名单与网页均已同步扩展 |
| 网页、API、数据库 | `POST /api/imports`；网页/GET API、JSON Path、只读 PostgreSQL SELECT/WITH；SSRF 内网拦截、主机允许列表、超时、大小与行数上限 |
| 正文/OCR/表格/结构 | pypdf + PyMuPDF/Tesseract OCR；pdfplumber、python-docx、openpyxl；保留页码、标题/章节、工作表、表格类型 |
| 清洗 | Unicode/空白统一、控制字符和替换字符清理、重复段落去重、常见样板文本过滤、多页重复页眉页脚移除 |
| Chunking | `fixed / paragraph / recursive / section / semantic`；统一支持 overlap；语义切分用 Embedding 相邻段落余弦突变点 |

## 2. 向量化与索引

- SentenceTransformers 文档批量向量化，查询使用 BGE 指令前缀，归一化后余弦检索；模型名、维度和缓存可配置。
- 选择 pgvector 作为唯一事实存储，避免业务库与 Milvus/Qdrant/Weaviate/FAISS/Pinecone 双写。模型表和迁移记录 Embedding 模型、维度、索引版本与 pipeline fingerprint。
- HNSW `vector_cosine_ops` 提供 ANN；`flat` 检索 profile 会在当前事务关闭 index/bitmap scan，用同一距离表达式执行精确 Flat。IVF 是 HNSW 的替代索引，本项目不同时维护两套等价 ANN 索引。
- Chunk 直接保存 workspace、owner、KB、文档、页码、章节、内容类型、来源 URL、Token 数、创建时间和扩展元数据；Document 保存部门、标签、来源类型及模型版本。

## 3. 查询理解与改写

- `services/query.py` 完成 Unicode/控制字符清理、常见错字修正、可配置领域术语字典、精确标识符识别和关键词提取。
- 多轮查询改写会结合最近历史，把指代问题改成独立问题；失败安全降级为原问题。
- Query Expansion/Multi-Query 生成同义表达并分别召回后 RRF 合并。
- HyDE 先生成短假设资料片段并独立向量召回。
- Auto Router 可跳过寒暄检索，并在 keyword/vector/hybrid/graph 间路由；知识库名称出现在问题中时自动选库，仍执行 RBAC 校验。

## 4. 检索

- 向量余弦召回 + PostgreSQL GIN 全文候选 + 候选集 BM25 评分 + RRF。
- 文件名/章节标题召回、实体图两跳召回、Multi-Query 多路向量召回。
- 元数据过滤支持用户/Workspace/KB、部门、标签、来源类型、内容类型、章节和创建时间。
- 父子检索和相邻 Chunk 扩展均已实现：子块命中后可返回父段落，普通模式按窗口补前后文。
- 轻量 Graph RAG 在入库时抽取关键词实体和共现关系，查询时匹配实体并遍历两跳关系回到证据 Chunk。它适合关系线索增强，不替代专门的人工知识图谱治理。

## 5. 重排序

- 首次召回数由 `RETRIEVAL_CANDIDATES` 控制，最终 Top-K 由 `RETRIEVAL_TOP_K` 控制。
- CrossEncoder 支持 BGE/Jina 等 SentenceTransformers 兼容 reranker；失败自动回退 RRF。
- 规则重排包含标题/章节命中、关键词命中与重复内容降权。
- 可选 LLM rerank 输出候选编号顺序；失败保留原排序。Cohere 属于另一远程提供方，不与本地默认模型强绑定。

## 6. 上下文构建

- `services/context.py` 对完全/高度相似 Chunk 去重，长片段提取问题相关句并保留相邻句。
- 父段落/相邻窗口补齐上下文；最终上下文按 `CONTEXT_MAX_TOKENS` 截断低优先内容。
- Lost-in-the-Middle 排序保留最高相关证据在开头、第二高证据在结尾。
- 来源事件保留 filename、page、section、source URL、chunk ID、内容类型和分数。

## 7. 生成

- 系统 Prompt 将检索文档标记为不可信数据，限制只能基于证据回答，要求 `[n]` 引用和资料不足拒答。
- SSE 流式输出，持久化问题/回答/来源/Token/首字与总耗时。
- 支持 Markdown 或 JSON；JSON 可传 JSON Schema，服务端在展示前执行结构校验。
- 引用编号、未引用事实、越界引用检查；可选 LLM Claim-Evidence entailment 二次验证，失败可 fail-closed。
- Redis 对话历史、滚动摘要和最近轮次共同处理多轮指代，同时控制历史长度。

## 8. 评测

- `scripts/eval_retrieval.py`：Precision@5、Recall@5、Hit Rate@5、MRR、nDCG@5、无答案误答率、P95。
- `scripts/eval_answers.py`：正确性、完整性、相关性、忠实度、引用正确性、引用完整性六维 LLM judge。
- 固定 golden set、质量门禁、消融脚本、RAG/Agent 对比和 Prompt Injection 发布门禁已接 CI 流程。
- Prometheus 记录请求/检索/首 Token/Token、拒答、缓存命中、入库各阶段、路由和上下文数量；UsageRecord 记录可配置模型单价估算成本。
- `POST /api/chat/feedback` 与 `/interactions` 记录点赞/点踩、复制、重新生成和追问指标，供在线评测/A-B 分析。
- 当前选择自建离线评测，未同时引入 RAGAS/DeepEval/TruLens/LangSmith，避免多个框架重复运行同一 judge。

## 9. 安全与权限

- JWT Access/Refresh、Refresh family 轮换/重放吊销、HttpOnly Cookie、WebSocket 一次性 ticket；另有可撤销/可过期、只显示一次明文的 API Key。
- Organization/Workspace RBAC + PostgreSQL FORCE RLS；文档和 Chunk 在检索 SQL 阶段做权限过滤，不依赖 Prompt。
- 外部数据接入执行 SSRF、协议、查询只读、大小/超时限制；API 请求头/数据库凭据不写审计。
- Prompt Injection 检测、Chunk 降权/移除、文档隔离区、只读 Agent 工具和不可信文档边界。
- 手机、身份证、邮箱、密码/Token 在审计元数据进入数据库前脱敏。
- 查询只记录不可逆 SHA-256，不把原问题写入审计；上传、导入、查询、删除、权限、API Key、反馈均写追加式防篡改审计链。

## 10. 工程化与运维

- Celery + Redis、Transactional Outbox、CAS 租约、批量 checkpoint、指数退避、死信/人工重放和对账恢复。
- 文件 SHA-256、数据库唯一约束、版本化 Chunk 唯一键和 pipeline fingerprint 保证幂等。
- 内容替换 `PUT /api/documents/{id}/content`、手工 reindex、删除 Outbox 和对象孤儿清理覆盖增删改同步。
- Embedding 模型/维度/切分配置哈希/激活索引版本均落库，升级可无停机重建。
- 进程级 Embedding LRU、Redis 历史/语义回答缓存；知识库变化整体失效。
- Logging、Prometheus/Grafana、OpenTelemetry/Tempo、健康检查、限流、超时、熔断、备用 LLM 和向量/关键词增强路径降级。
- Docker/Docker Compose/Caddy、Alembic、环境变量和 GitHub Actions 已提供。Kubernetes/Nginx 是替代部署面，不是当前 Compose 主链路的运行依赖。

## 端到端链路

```text
上传/网页/API/只读数据库
→ 解析、OCR、表格与结构提取
→ 清洗、去重、页眉页脚处理
→ 结构化/语义 Chunk + overlap
→ Embedding 缓存与 pgvector HNSW
→ 查询清洗、改写、扩展、HyDE、路由
→ 向量 + BM25 + 标题 + 图关系多路召回
→ RRF + 规则/CrossEncoder/LLM Rerank
→ 去重、相邻补全、压缩、Token 预算与 Lost-in-the-Middle
→ 证据门禁、SSE 生成、引用与二次验证
→ 离线门禁、在线反馈、成本与性能观测
```

## 上线说明

1. 执行数据库迁移：`alembic upgrade head`。
2. 重新构建 API 与 Worker 镜像，以安装 OCR 和新增解析依赖。
3. 执行 `python scripts/reindex_documents.py`，为历史文档批量触发版本化重建；可用 `--workspace-id` 限定范围。
4. OCR、HyDE、LLM Rerank、Graph RAG 均可通过环境变量按成本与延迟要求启停。

## 多租户与并发保障

- API 使用应用层 RBAC 与 PostgreSQL FORCE RLS 双重过滤；租户身份通过事务级 `SET LOCAL` 注入，每次提交后自动恢复，连接归还池时不会携带上一用户身份。
- 语义缓存按 `用户 × 知识库` 隔离，对话缓存按不可猜测的会话 ID 隔离，并在数据库层再次校验 owner。
- Redis 原子容量租约同时限制全局、单用户和单会话并发：不同用户可以并行，同一会话只允许一个生成任务，避免消息历史发生分叉。
- 限流按认证凭据的不可逆摘要计数，不再让同一 NAT/IP 下的多个用户互相占用额度；多个 API 实例共享 Redis 计数。
- 工作区容量检查使用 PostgreSQL advisory transaction lock，将“检查配额 + 写入文档记录”串行化，防止并发上传共同越过容量上限。
- 文档重建、取消、删除和元数据修改使用行锁/CAS；Outbox 使用 `FOR UPDATE SKIP LOCKED`，入库 Worker 使用处理租约与版本切换 CAS，重复投递不会激活两套索引。
