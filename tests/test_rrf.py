"""RRF 融合与关键词抽取单元测试。"""
from app.services.retriever import extract_keywords, rrf_fuse


def test_rrf_both_lists_ranked_higher():
    """两路都召回的文档，融合分应高于只出现在一路的文档。"""
    vector_ranking = ["a", "b", "c"]
    keyword_ranking = ["b", "d"]
    scores = rrf_fuse([vector_ranking, keyword_ranking], k=60)
    assert scores["b"] > scores["a"]
    assert scores["b"] > scores["d"]


def test_rrf_empty_ranking():
    scores = rrf_fuse([[], ["x"]], k=60)
    assert "x" in scores


def test_extract_keywords_filters_stopwords():
    terms = extract_keywords("请问怎么申请退款流程")
    assert "退款" in terms or "退款流程" in terms or "申请" in terms
    assert "请问" not in terms
    assert "怎么" not in terms


def test_extract_keywords_dedup_and_limit():
    terms = extract_keywords("退款 退款 退款 " * 10, max_terms=8)
    assert len(terms) <= 8
    assert len(terms) == len(set(terms))
