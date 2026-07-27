"""文本切片：递归字符切分，段落优先、句子兜底、带重叠。

为什么不是简单按固定长度切？
- 固定长度会把一句话拦腰截断，检索出来的片段语义不完整；
- 这里先按段落（\n\n）切，段落太长再按句子切，句子还太长才硬切；
- 相邻片段保留 overlap 重叠，避免答案正好落在切分边界上被截掉。
"""
import re

# 中英文句末标点
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|(?<=\.)\s+")


def split_text(text: str, chunk_size: int = 512, overlap: int = 64) -> list[str]:
    """把长文本切成不超过 chunk_size 的片段，相邻片段有 overlap 字符重叠。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须为正数")
    if overlap >= chunk_size:
        raise ValueError("overlap 必须小于 chunk_size")

    text = _normalize(text)
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]

    # 第一层：按段落切
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    # 第二层：段落超长则按句子拆成小单元
    units: list[str] = []
    for para in paragraphs:
        if len(para) <= chunk_size:
            units.append(para)
        else:
            units.extend(_split_long_paragraph(para, chunk_size))

    # 第三层：把小单元贪心合并到 chunk_size，并做重叠
    return _merge_units(units, chunk_size, overlap)


def _normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_long_paragraph(para: str, chunk_size: int) -> list[str]:
    sentences = [s for s in _SENTENCE_SPLIT.split(para) if s and s.strip()]
    units: list[str] = []
    for sent in sentences:
        sent = sent.strip()
        if len(sent) <= chunk_size:
            units.append(sent)
        else:  # 单句都超长（如无标点的表格文本），硬切
            units.extend(sent[i : i + chunk_size] for i in range(0, len(sent), chunk_size))
    return units


def _merge_units(units: list[str], chunk_size: int, overlap: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for unit in units:
        candidate = f"{current}\n{unit}" if current else unit
        if len(candidate) <= chunk_size:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # 从上一片尾部取 overlap 字符作为新片开头，保持上下文连续
            tail = current[-overlap:] if current and overlap > 0 else ""
            candidate = f"{tail}\n{unit}" if tail else unit
            current = candidate if len(candidate) <= chunk_size else unit[:chunk_size]
    if current:
        chunks.append(current)
    return chunks
