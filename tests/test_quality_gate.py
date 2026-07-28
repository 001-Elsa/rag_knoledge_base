from scripts.check_quality_gate import check_quality

BASELINE = {
    "hit_rate_at_5": 0.90,
    "mrr": 0.80,
    "no_answer_false_positive_rate": 0.10,
    "p95_retrieval_ms": 100,
}


def test_quality_gate_accepts_improvement():
    current = {
        "hit_rate_at_5": 0.92,
        "mrr": 0.84,
        "no_answer_false_positive_rate": 0.05,
        "p95_retrieval_ms": 110,
    }
    assert check_quality(BASELINE, current) == []


def test_quality_gate_reports_each_regression():
    current = {
        "hit_rate_at_5": 0.80,
        "mrr": 0.70,
        "no_answer_false_positive_rate": 0.20,
        "p95_retrieval_ms": 150,
    }
    assert len(check_quality(BASELINE, current)) == 4
