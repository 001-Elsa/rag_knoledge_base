"""文档入库流水线（Celery 异步任务）：解析 → 切片 → 向量化 → 写入 pgvector。

为什么放异步任务里？
- 大 PDF 解析 + 向量化可能耗时几十秒到几分钟，放在上传接口里会把请求阻塞死；
- 上传接口只负责存文件 + 建记录（毫秒级返回），重活交给 worker；
- 状态变化通过 Redis 发布订阅 → WebSocket 实时推给前端（不再轮询）。
"""
import logging

import jieba
from sqlalchemy import func

from app.config import settings
from app.db import SyncSessionLocal
from app.models import Chunk, DocStatus, Document
from app.services.chunker import split_text
from app.services.embedder import embed_documents
from app.services.notify import document_event, publish_sync
from app.services.parser import parse_file
from app.tasks import celery_app

logger = logging.getLogger(__name__)


def _tokenize(text: str) -> str:
    return " ".join(t for t in jieba.cut_for_search(text) if t.strip())


@celery_app.task(name="ingest_document", bind=True, max_retries=2, default_retry_delay=10)
def ingest_document(self, document_id: str) -> dict:
    with SyncSessionLocal() as db:
        doc = db.get(Document, document_id)
        if doc is None:
            logger.warning("文档不存在，跳过: %s", document_id)
            return {"ok": False, "reason": "not_found"}

        doc.status = DocStatus.processing
        db.commit()
        publish_sync(doc.owner_id, document_event(doc.id, "processing", filename=doc.filename))

        try:
            # 1. 解析（PDF 携带页码）
            segments = parse_file(doc.filepath)
            if not any(text.strip() for text, _ in segments):
                raise ValueError("解析结果为空（可能是扫描版 PDF，需要 OCR）")

            # 2. 切片：逐段切分，保留每片来源页码
            pieces: list[tuple[str, int | None]] = []
            for text, page in segments:
                for piece in split_text(text, settings.chunk_size, settings.chunk_overlap):
                    pieces.append((piece, page))
            if not pieces:
                raise ValueError("切片结果为空")

            # 3. 向量化（批量）
            vectors = embed_documents([p for p, _ in pieces])

            # 4. 入库（先清旧片段，保证重跑幂等）
            db.query(Chunk).filter(Chunk.document_id == doc.id).delete()
            db.add_all(
                Chunk(
                    document_id=doc.id,
                    owner_id=doc.owner_id,
                    kb_id=doc.kb_id,
                    seq=i,
                    page=page,
                    content=piece,
                    # 全文检索：jieba 分词后交给 to_tsvector('simple')，天然支持中文倒排
                    content_tokens=func.to_tsvector("simple", _tokenize(piece)),
                    embedding=vector,
                )
                for i, ((piece, page), vector) in enumerate(zip(pieces, vectors))
            )
            doc.chunk_count = len(pieces)
            doc.status = DocStatus.ready
            doc.error = None
            db.commit()

            # 知识库内容变了，旧的语义缓存答案可能过时 → 整体失效
            from app.services.semantic_cache import invalidate_user_sync

            invalidate_user_sync(doc.owner_id)
            publish_sync(
                doc.owner_id,
                document_event(doc.id, "ready", filename=doc.filename, chunk_count=len(pieces)),
            )
            logger.info("文档入库完成: %s（%d 个切片）", doc.filename, len(pieces))
            return {"ok": True, "chunks": len(pieces)}

        except Exception as exc:
            db.rollback()
            doc = db.get(Document, document_id)
            if doc is not None:
                doc.status = DocStatus.failed
                doc.error = str(exc)[:2000]
                db.commit()
                publish_sync(doc.owner_id, document_event(doc.id, "failed", filename=doc.filename, error=doc.error))
            logger.exception("文档入库失败: %s", document_id)
            raise self.retry(exc=exc)
