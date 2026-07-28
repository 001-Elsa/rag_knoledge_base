"""文档解析：把 PDF / Word / Markdown / TXT 解析为「文本段 + 页码」序列。

返回 [(text, page)]：PDF 每页一段（page 从 1 起），其他类型整体一段（page=None）。
保留页码信息是为了引用能精确到"某文件第几页"，用户可以回原文核对。
"""
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from app.config import settings

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".md", ".txt"}

Segment = tuple[str, int | None]  # (文本, 页码)


def parse_file(filepath: str) -> list[Segment]:
    """按扩展名分发解析器。不支持的类型抛 ValueError。"""
    path = Path(filepath)
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _parse_pdf(path)
    if ext == ".docx":
        return [(_parse_docx(path), None)]
    if ext in (".md", ".txt"):
        return [(path.read_text(encoding="utf-8", errors="ignore"), None)]
    raise ValueError(f"不支持的文件类型: {ext}（支持 {sorted(SUPPORTED_EXTENSIONS)}）")


def _parse_pdf(path: Path) -> list[Segment]:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if len(reader.pages) > settings.max_document_pages:
        raise ValueError(
            f"PDF 页数超过限制（{len(reader.pages)} > {settings.max_document_pages}）"
        )
    return [(page.extract_text() or "", i) for i, page in enumerate(reader.pages, start=1)]


def _parse_docx(path: Path) -> str:
    import docx

    try:
        with ZipFile(path) as archive:
            expanded_bytes = sum(info.file_size for info in archive.infolist())
    except BadZipFile as exc:
        raise ValueError("DOCX 压缩包损坏") from exc
    max_expanded = settings.max_uncompressed_mb * 1024 * 1024
    if expanded_bytes > max_expanded:
        raise ValueError(
            f"DOCX 解压大小超过限制（{settings.max_uncompressed_mb}MB）"
        )
    document = docx.Document(str(path))
    parts: list[str] = [p.text for p in document.paragraphs if p.text.strip()]
    # 表格内容也要抽取，否则丢信息
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)
