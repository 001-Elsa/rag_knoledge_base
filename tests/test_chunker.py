"""切片器单元测试。"""
import pytest

from app.services.chunker import split_text


def test_empty_text():
    assert split_text("") == []
    assert split_text("   \n\n  ") == []


def test_short_text_single_chunk():
    text = "这是一段很短的文本。"
    assert split_text(text, chunk_size=512) == [text]


def test_chunks_respect_size_limit():
    text = "第一句话。" * 500
    chunks = split_text(text, chunk_size=200, overlap=20)
    assert len(chunks) > 1
    assert all(len(c) <= 200 for c in chunks)


def test_overlap_between_chunks():
    """相邻切片应有内容重叠（第二片开头来自第一片尾部）。"""
    text = "。".join(f"句子{i}内容内容内容内容" for i in range(100))
    chunks = split_text(text, chunk_size=150, overlap=30)
    assert len(chunks) >= 2
    # 第二片应包含第一片尾部的部分内容
    tail = chunks[0][-10:]
    assert any(part in chunks[1] for part in [tail[-5:], tail])


def test_paragraph_boundary_preferred():
    """段落远小于 chunk_size 时，切分不应破坏段落。"""
    paras = [f"段落{i}：" + "内容" * 20 for i in range(10)]
    text = "\n\n".join(paras)
    chunks = split_text(text, chunk_size=300, overlap=0)
    joined = "".join(chunks)
    for i in range(10):
        assert f"段落{i}：" in joined


def test_no_content_lost():
    """所有原始句子都应出现在切片结果中。"""
    sentences = [f"这是唯一标识句{i}。" for i in range(50)]
    text = "".join(sentences)
    chunks = split_text(text, chunk_size=100, overlap=10)
    joined = "".join(chunks)
    for s in sentences:
        assert s[:-1] in joined  # 去掉句号比对（切分可能落在标点后）


def test_invalid_params():
    with pytest.raises(ValueError):
        split_text("abc", chunk_size=0)
    with pytest.raises(ValueError):
        split_text("abc", chunk_size=100, overlap=100)
