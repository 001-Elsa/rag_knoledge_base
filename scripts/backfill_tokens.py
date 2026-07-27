"""回填存量切片的全文检索分词（升级到含 tsvector 的版本后跑一次即可）。

用法（能连上数据库即可，Docker 部署时 5432 已映射本机）：
  python scripts/backfill_tokens.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import jieba
from sqlalchemy import func, select, update

from app.db import SyncSessionLocal
from app.models import Chunk


def tokenize(text: str) -> str:
    return " ".join(t for t in jieba.cut_for_search(text) if t.strip())


def main() -> int:
    updated = 0
    with SyncSessionLocal() as db:
        rows = db.execute(
            select(Chunk.id, Chunk.content).where(Chunk.content_tokens.is_(None))
        ).all()
        print(f"待回填切片: {len(rows)}")
        for i, (chunk_id, content) in enumerate(rows, 1):
            db.execute(
                update(Chunk)
                .where(Chunk.id == chunk_id)
                .values(content_tokens=func.to_tsvector("simple", tokenize(content)))
            )
            updated += 1
            if i % 200 == 0:
                db.commit()
                print(f"  已处理 {i}/{len(rows)}")
        db.commit()
    print(f"回填完成: {updated} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
