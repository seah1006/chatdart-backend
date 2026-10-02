"""
재무 평가 헬퍼 함수 유닛 테스트

외부 의존성 없음 — DB, HTTP, DART API 모두 불필요.
SimpleNamespace로 ORM 객체를 흉내 낸다.
"""
from types import SimpleNamespace

from app.services.analysis import (
    safe_div,
    evaluate_growth,
    evaluate_stability,
    evaluate_profitability,
    compute_warning_flags,
)


# ── safe_div (3개) ───────────────────────────────────────────────────────────

def test_safe_div_returns_correct_result():
    assert safe_div(10.0, 2.0) == 5.0


def test_safe_div_zero_denominator_returns_default():
    assert safe_div(10.0, 0.0) == 0.0


def test_safe_div_zero_denominator_custom_default():
    assert safe_div(10.0, 0.0, default=-1.0) == -1.0


# ── evaluate_growth (3개) ────────────────────────────────────────────────────

def test_evaluate_growth_revenue_increased():
    latest = SimpleNamespace(revenue=1_200)
    previous = SimpleNamespace(revenue=1_000)
    assert evaluate_growth(latest, previous) == "양호"


def test_evaluate_growth_revenue_decreased():
    latest = SimpleNamespace(revenue=800)
    previous = SimpleNamespace(revenue=1_000)
    assert evaluate_growth(latest, previous) == "부진"


def test_evaluate_growth_no_previous_returns_데이터부족():
    latest = SimpleNamespace(revenue=1_000)
    assert evaluate_growth(latest, None) == "데이터 부족"


# ── evaluate_stability (3개) ─────────────────────────────────────────────────

def test_evaluate_stability_low_debt_ratio_returns_우수():
    latest = SimpleNamespace(equity=1_000, total_liabilities=500)
    assert evaluate_stability(latest) == "우수"


def test_evaluate_stability_high_debt_ratio_returns_주의():
    latest = SimpleNamespace(equity=1_000, total_liabilities=1_500)
    assert evaluate_stability(latest) == "주의"


def test_evaluate_stability_zero_equity_returns_데이터부족():
    latest = SimpleNamespace(equity=0, total_liabilities=500)
    assert evaluate_stability(latest) == "데이터 부족"


# ── evaluate_profitability (3개) ─────────────────────────────────────────────

def test_evaluate_profitability_margin_improved():
    latest = SimpleNamespace(operating_profit=100, revenue=1_000)
    previous = SimpleNamespace(operating_profit=80, revenue=1_000)
    assert evaluate_profitability(latest, previous) == "개선"


def test_evaluate_profitability_margin_declined():
    latest = SimpleNamespace(operating_profit=80, revenue=1_000)
    previous = SimpleNamespace(operating_profit=100, revenue=1_000)
    assert evaluate_profitability(latest, previous) == "부진"


def test_evaluate_profitability_no_previous_returns_데이터부족():
    latest = SimpleNamespace(operating_profit=100, revenue=1_000)
    assert evaluate_profitability(latest, None) == "데이터 부족"


# ── compute_warning_flags (7개) ──────────────────────────────────────────────

def _fs(**kwargs):
    defaults = dict(
        equity=1_000, total_liabilities=500, net_income=100,
        operating_profit=50, revenue=1_000,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_warning_flags_empty_for_healthy_company():
    records = [_fs()]
    assert compute_warning_flags(records) == []


def test_warning_flags_capital_impairment():
    records = [_fs(equity=0)]
    assert "자본잠식" in compute_warning_flags(records)


def test_warning_flags_negative_equity():
    records = [_fs(equity=-100)]
    assert "자본잠식" in compute_warning_flags(records)


def test_warning_flags_high_debt_ratio():
    records = [_fs(equity=100, total_liabilities=400)]  # 400% 부채비율
    flags = compute_warning_flags(records)
    assert any("부채비율" in f for f in flags)


def test_warning_flags_net_loss():
    records = [_fs(net_income=-1)]
    assert "당기순손실" in compute_warning_flags(records)


def test_warning_flags_three_year_operating_loss():
    records = [_fs(operating_profit=-10), _fs(operating_profit=-20), _fs(operating_profit=-5)]
    assert "3년 연속 영업손실" in compute_warning_flags(records)


def test_warning_flags_revenue_decline():
    latest = _fs(revenue=600)
    previous = _fs(revenue=1_000)  # 40% 감소
    flags = compute_warning_flags([latest, previous])
    assert any("급감" in f for f in flags)
