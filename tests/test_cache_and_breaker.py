"""语义缓存相似度与熔断器单元测试。"""
import time

from app.services.llm import CircuitBreaker
from app.services.semantic_cache import cosine_similarity, is_eligible


def test_cosine_identical_vectors():
    v = [0.1, 0.5, -0.3]
    assert abs(cosine_similarity(v, v) - 1.0) < 1e-9


def test_cosine_orthogonal_vectors():
    assert abs(cosine_similarity([1, 0], [0, 1])) < 1e-9


def test_cosine_zero_vector():
    assert cosine_similarity([0, 0], [1, 1]) == 0.0


def test_semantic_cache_only_accepts_context_free_questions():
    assert is_eligible([])
    assert not is_eligible([{"role": "user", "content": "产品 A"}])


def test_breaker_opens_after_threshold():
    breaker = CircuitBreaker(fail_threshold=3, cooldown_seconds=60)
    breaker.check()  # 初始关闭，不抛
    for _ in range(3):
        breaker.record_failure()
    try:
        breaker.check()
        raise AssertionError("熔断后 check 应抛异常")
    except RuntimeError:
        pass


def test_breaker_success_resets_count():
    breaker = CircuitBreaker(fail_threshold=3, cooldown_seconds=60)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()  # 归零
    breaker.record_failure()
    breaker.record_failure()
    breaker.check()  # 未达到阈值，不应熔断


def test_breaker_recovers_after_cooldown(monkeypatch):
    breaker = CircuitBreaker(fail_threshold=1, cooldown_seconds=10)
    breaker.record_failure()  # 打开
    # 快进时间越过冷却期
    real = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: real + 11)
    breaker.check()  # 冷却结束，恢复调用
