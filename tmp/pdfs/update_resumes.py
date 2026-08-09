from __future__ import annotations

from pathlib import Path
import re
import shutil

import fitz
from fontTools import subset
from fontTools.ttLib import TTFont


PDF_DIR = Path(r"D:\a_Intern\resume-pdf")
WORK_DIR = Path(r"D:\a_projects\rag-knowledge-base\tmp\pdfs\updated")
BACKUP_DIR = Path(r"D:\a_projects\rag-knowledge-base\tmp\pdfs\originals")

REGULAR_FONT_FILE = Path(r"C:\Windows\Fonts\simsun.ttc")
SUBSET_FONT_FILE = Path(r"D:\a_projects\rag-knowledge-base\tmp\pdfs\resume_simsun_subset.ttf")
FONT_SIZE = 11.0
LINE_HEIGHT = 15.2
TITLE_X = 25.5
BODY_X = 42.0
BULLET_X = 25.5
RIGHT_X = 570.0

RESUMES = [
    {
        "filename": "韩桢-后端-中山大学软件工程学院-Resume.pdf",
        "rect": (23.0, 474.0, 575.0, 570.0),
        "title": "企业级 RAG 知识库平台｜补充项目",
        "bullets": [
            "项目概述：面向企业文档管理，构建可靠异步入库、多租户隔离和无停机索引升级的知识库平台。",
            "技术栈：FastAPI、PostgreSQL/pgvector、Celery、Redis、MinIO、RLS、OpenTelemetry。",
            "Document、Outbox 和审计同事务写入；Dispatcher 补偿投递，Worker 以 CAS 租约、心跳和幂等写入恢复中断任务。",
            "索引新版本写全后原子切换；组织、工作空间和 RLS 实现多租户隔离。",
        ],
    },
    {
        "filename": "韩桢-AI应用-中山大学软件工程学院-Resume.pdf",
        "rect": (23.0, 474.0, 575.0, 588.0),
        "title": "企业级 RAG 知识库平台｜补充项目",
        "bullets": [
            "项目概述：面向企业知识问答，构建可评测的 RAG 平台，聚焦混合检索、可信生成与安全防护。",
            "技术栈：pgvector、PostgreSQL 全文检索、RRF、CrossEncoder、Parent-Child Retrieval、Celery。",
            "以向量与全文召回经 RRF 融合，支持 Parent-Child、多查询改写和 CrossEncoder 重排，提升跨段与复杂问题召回。",
            "证据不足时确定性拒答，并做引用校验与间接 Prompt Injection 防护；以固定集度量召回、排序、无答案误答率和 P95。",
        ],
    },
    {
        "filename": "韩桢-python后端-中山大学软件工程学院-Resume.pdf",
        "rect": (23.0, 331.0, 575.0, 459.0),
        "title": "企业级 RAG 知识库与文档处理平台",
        "bullets": [
            "项目概述：面向企业文档处理，使用 Python 异步后端构建可靠的文档入库与检索流水线。",
            "技术栈：FastAPI、SQLAlchemy、PostgreSQL/pgvector、Celery、Redis、MinIO、Alembic、OpenTelemetry。",
            "解析、切片、嵌入和索引交由 Celery；API 使用 AsyncSession，耗时任务不阻塞上传与查询。",
            "Document、Outbox 与审计同事务写入；Worker 以 CAS 租约、心跳和 checkpoint 支持中断恢复与幂等。",
            "索引新版本全量写入后原子切换，重建时查询不中断；组织、工作空间与 RLS 隔离租户。",
            "接入 OpenTelemetry 追踪 FastAPI、SQLAlchemy、Redis 与 Celery 链路，监控入库阶段、队列延迟和重试。",
        ],
    },
]


def tokens(text: str) -> list[str]:
    """Keep ASCII technology names intact while allowing natural CJK wrapping."""
    return re.findall(r"[A-Za-z0-9@+._/\-]+|.", text)


def build_subset_font() -> Path:
    """Embed only the glyphs needed by these three edits, not the full 18 MB TTC."""
    text = "·" + "".join(
        str(resume["title"]) + "".join(str(bullet) for bullet in resume["bullets"])
        for resume in RESUMES
    )
    font = TTFont(str(REGULAR_FONT_FILE), fontNumber=0)
    options = subset.Options()
    options.name_IDs = [1, 2, 3, 4, 6]
    subsetter = subset.Subsetter(options=options)
    subsetter.populate(text=text)
    subsetter.subset(font)
    font.save(str(SUBSET_FONT_FILE))
    return SUBSET_FONT_FILE


def wrap(text: str, font: fitz.Font, width: float) -> list[str]:
    lines: list[str] = []
    current = ""
    for token in tokens(text):
        candidate = current + token
        if current and font.text_length(candidate, fontsize=FONT_SIZE) > width:
            lines.append(current)
            current = token
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def update_one(spec: dict[str, object]) -> Path:
    source = PDF_DIR / str(spec["filename"])
    output = WORK_DIR / source.name
    backup = BACKUP_DIR / source.name
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, backup)
    shutil.copy2(source, output)

    doc = fitz.open(output)
    page = doc[0]
    page.insert_font(fontname="ResumeRegular", fontfile=str(SUBSET_FONT_FILE))
    regular = fitz.Font(fontfile=str(SUBSET_FONT_FILE))

    left, top, right, bottom = spec["rect"]  # type: ignore[misc]
    page.draw_rect(fitz.Rect(left, top, right, bottom), color=None, fill=(1, 1, 1), overlay=True)

    baseline = top + 11.0
    # SimSun's regular face has complete CJK coverage. A second, slight offset keeps
    # the project heading visually aligned with the original bold heading.
    page.insert_text((TITLE_X, baseline), str(spec["title"]), fontsize=FONT_SIZE, fontname="ResumeRegular", color=(0, 0, 0), overlay=True)
    page.insert_text((TITLE_X + 0.18, baseline), str(spec["title"]), fontsize=FONT_SIZE, fontname="ResumeRegular", color=(0, 0, 0), overlay=True)
    baseline += LINE_HEIGHT
    used_lines = 1
    for bullet in spec["bullets"]:  # type: ignore[assignment]
        lines = wrap(str(bullet), regular, RIGHT_X - BODY_X)
        page.insert_text((BULLET_X, baseline), "·", fontsize=FONT_SIZE, fontname="ResumeRegular", color=(0, 0, 0), overlay=True)
        for index, line in enumerate(lines):
            page.insert_text((BODY_X, baseline + index * LINE_HEIGHT), line, fontsize=FONT_SIZE, fontname="ResumeRegular", color=(0, 0, 0), overlay=True)
        baseline += LINE_HEIGHT * len(lines)
        used_lines += len(lines)

    if baseline - LINE_HEIGHT + 3 > bottom:
        raise RuntimeError(f"Text overflows target area in {source.name}: {used_lines} lines")
    doc.saveIncr()
    doc.close()
    print(f"{source.name}: {used_lines} lines -> {output}")
    return output


if __name__ == "__main__":
    build_subset_font()
    for resume in RESUMES:
        update_one(resume)
