"""全文检索查询构造与摘要触发边界的单元测试。"""
from app.services.retriever import build_tsquery, extract_keywords


def test_build_tsquery_or_join():
    assert build_tsquery(["退款", "流程"]) == "退款 | 流程"


def test_build_tsquery_filters_special_chars():
    """tsquery 语法字符（& | ! ( ) : 引号等）必须被过滤，防注入/防语法错误。"""
    result = build_tsquery(["退款", "a|b", "x&y", "(注入)", "正常词"])
    assert "a|b" not in result
    assert "x&y" not in result
    assert "(注入)" not in result
    assert "退款" in result and "正常词" in result


def test_build_tsquery_empty():
    assert build_tsquery([]) == ""
    assert build_tsquery(["!!!", "|||"]) == ""


def test_keywords_feed_tsquery():
    """extract_keywords 的输出应能直接安全地进入 build_tsquery。"""
    keywords = extract_keywords("请问 XR-500 的保修期是多久")
    q = build_tsquery(keywords)
    for part in q.split(" | "):
        assert part.strip()


def test_summary_boundary_logic():
    """滚动摘要的触发边界：总数达到阈值且有新的旧轮次才压缩。"""
    trigger, keep = 12, 6

    def should_summarize(total: int, summary_upto: int) -> bool:
        boundary = total - keep
        return total >= trigger and boundary > summary_upto

    assert not should_summarize(total=10, summary_upto=0)   # 未到阈值
    assert should_summarize(total=12, summary_upto=0)       # 刚到阈值
    assert not should_summarize(total=12, summary_upto=6)   # 已覆盖，无新增
    assert should_summarize(total=20, summary_upto=6)       # 有新增旧轮次
