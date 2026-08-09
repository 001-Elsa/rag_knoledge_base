from types import SimpleNamespace

import pytest

from app.routers.imports import _allowed_host, _validate_database_query
from app.services.chunker import split_segments
from app.services.context import prepare_context
from app.services.parser import ParsedSegment, _remove_repeated_margins, clean_text, parse_document
from app.services.query import clean_query, route_query
from app.services.redaction import redact_text
from app.services.retriever import RetrievedChunk, _bm25_score


def _chunk(identifier: str, content: str, score: float = 1.0) -> RetrievedChunk:
    return RetrievedChunk(identifier, "doc", "source.md", 0, None, content, score)


def test_cleaning_removes_controls_boilerplate_and_duplicate_lines():
    value = "标题\x00\n版权所有\n有效内容足够长 123456\n有效内容足够长 123456"
    assert clean_text(value) == "标题\n\n有效内容足够长 123456"


def test_repeated_pdf_headers_and_footers_are_removed():
    pages = [f"公司机密\n第 {index} 页\n正文 {index}\n版权所有 2026" for index in range(1, 5)]
    cleaned = _remove_repeated_margins(pages)
    assert all("公司机密" not in page and "版权所有" not in page for page in cleaned)
    assert all("正文" in page for page in cleaned)


def test_markdown_preserves_sections(tmp_path):
    path = tmp_path / "guide.md"
    path.write_text("# 安装\n第一步。\n\n## 配置\n第二步。", encoding="utf-8")
    segments = parse_document(str(path))
    assert [segment.section for segment in segments] == ["安装", "配置"]


def test_html_removes_navigation_and_preserves_heading(tmp_path):
    path = tmp_path / "page.html"
    path.write_text("<html><nav>无关菜单</nav><main><h1>退款</h1><p>七天内可退。</p></main></html>", encoding="utf-8")
    segments = parse_document(str(path))
    assert segments[0].section == "退款"
    assert "七天内可退" in segments[0].text
    assert "无关菜单" not in segments[0].text


def test_excel_preserves_sheet_and_table(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    path = tmp_path / "prices.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "产品价格"
    sheet.append(["产品", "价格"])
    sheet.append(["A", 100])
    workbook.save(path)
    segments = parse_document(str(path))
    assert segments[0].section == "产品价格"
    assert "产品 | 价格" in segments[0].text
    assert "A | 100" in segments[0].text


@pytest.mark.parametrize("strategy", ["fixed", "paragraph", "recursive", "section"])
def test_all_non_semantic_chunk_strategies_keep_structure(strategy):
    segments = [ParsedSegment("第一段。\n\n第二段。" * 20, page=2, section="章节 A")]
    chunks = split_segments(segments, strategy=strategy, chunk_size=80, overlap=10)
    assert chunks
    assert all(chunk.page == 2 and chunk.section == "章节 A" for chunk in chunks)
    assert all(len(chunk.content) <= 80 for chunk in chunks)


def test_query_cleaning_and_routing(monkeypatch):
    monkeypatch.setattr("app.services.query.settings.query_term_dictionary", "SaaS=软件即服务")
    assert clean_query("  SaaS\x00 帐户  ") == "软件即服务 账户"
    exact = route_query("错误码 ERR-500 是什么", requested_profile="auto")
    assert exact.retrieval_mode == "keyword"
    graph = route_query("订单和退款有什么关系", requested_profile="auto")
    assert graph.retrieval_mode == "graph"
    routed = route_query(
        "产品库里的退款规则",
        requested_profile="auto",
        knowledge_bases=[SimpleNamespace(id="kb-1", name="产品库")],
    )
    assert routed.kb_id == "kb-1"


def test_context_deduplicates_compresses_and_respects_budget(monkeypatch):
    monkeypatch.setattr("app.services.context.settings.context_max_tokens", 80)
    monkeypatch.setattr("app.services.context.settings.context_compression_min_chars", 20)
    chunks = [
        _chunk("1", "退款期限是七天。无关说明。" * 20),
        _chunk("2", "退款期限是七天。无关说明。" * 20, 0.9),
        _chunk("3", "到账需要三个工作日。" * 20, 0.8),
    ]
    prepared = prepare_context(chunks, "退款期限")
    assert len(prepared) <= 2
    assert sum(len(chunk.content) // 2 for chunk in prepared) <= 80


def test_bm25_prefers_repeated_exact_term():
    rows = [(SimpleNamespace(content="错误码 E500 E500 E500"),), (SimpleNamespace(content="一般错误说明 E500"),)]
    first = _bm25_score(rows[0][0].content, ["e500"], rows)
    second = _bm25_score(rows[1][0].content, ["e500"], rows)
    assert first > second


def test_sensitive_values_are_masked():
    redacted = redact_text("手机号 13812345678 邮箱 alice@example.com password=hunter2")
    assert "13812345678" not in redacted
    assert "alice@example.com" not in redacted
    assert "hunter2" not in redacted


def test_external_import_blocks_ssrf_and_mutating_sql():
    with pytest.raises(ValueError):
        _allowed_host("localhost")
    assert _validate_database_query("SELECT id FROM products") == "SELECT id FROM products"
    with pytest.raises(ValueError):
        _validate_database_query("DELETE FROM products")
    with pytest.raises(ValueError):
        _validate_database_query("SELECT 1; DROP TABLE users")
