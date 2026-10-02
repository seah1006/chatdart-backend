"""app/services/analysis.py 직접 단위 테스트."""
import math
import pytest
from unittest.mock import MagicMock

from app.services.analysis import (
    safe_div,
    evaluate_growth,
    evaluate_stability,
    evaluate_profitability,
    compute_warning_flags,
    compute_interpretation_points,
    compute_metrics,
)


def _stmt(**kwargs):
    """테스트용 FinancialStatement 모의 객체."""
    defaults = dict(
        revenue=0, operating_profit=0, net_income=0,
        total_assets=0, total_liabilities=0, equity=0, cash=0,
        cost_of_sales=0, gross_profit=0, sga=0,
        source_report=None,
    )
    defaults.update(kwargs)
    obj = MagicMock()
    for k, v in defaults.items():
        setattr(obj, k, v)
    return obj


# ── safe_div ──────────────────────────────────────────────────────────────────

def test_safe_div_normal():
    assert safe_div(10, 4) == pytest.approx(2.5)


def test_safe_div_zero_denominator_returns_default():
    assert safe_div(100, 0) == 0.0


def test_safe_div_custom_default():
    assert safe_div(5, 0, default=float("inf")) == float("inf")


def test_safe_div_negative_numerator():
    assert safe_div(-50, 10) == pytest.approx(-5.0)


# ── evaluate_growth ───────────────────────────────────────────────────────────

def test_evaluate_growth_no_previous_returns_데이터부족():
    latest = _stmt(revenue=100)
    assert evaluate_growth(latest, None) == "데이터 부족"


def test_evaluate_growth_revenue_increased_returns_양호():
    latest = _stmt(revenue=200)
    previous = _stmt(revenue=100)
    assert evaluate_growth(latest, previous) == "양호"


def test_evaluate_growth_revenue_decreased_returns_부진():
    latest = _stmt(revenue=80)
    previous = _stmt(revenue=100)
    assert evaluate_growth(latest, previous) == "부진"


def test_evaluate_growth_revenue_equal_returns_보합():
    """매출 동일하면 성장률 0%이므로 보합."""
    latest = _stmt(revenue=100)
    previous = _stmt(revenue=100)
    assert evaluate_growth(latest, previous) == "보합"


def test_evaluate_growth_small_increase_returns_보합():
    latest = _stmt(revenue=104)
    previous = _stmt(revenue=100)
    assert evaluate_growth(latest, previous) == "보합"


def test_evaluate_growth_threshold_exactly_5pct_returns_양호():
    latest = _stmt(revenue=105)
    previous = _stmt(revenue=100)
    assert evaluate_growth(latest, previous) == "양호"


def test_evaluate_growth_threshold_exactly_minus5pct_returns_부진():
    latest = _stmt(revenue=95)
    previous = _stmt(revenue=100)
    assert evaluate_growth(latest, previous) == "부진"


@pytest.mark.parametrize(
    ("latest_net_income", "previous_net_income", "expected"),
    [(110, 100, "양호"), (90, 100, "부진"), (104, 100, "보합"), (100, None, "데이터 부족")],
)
def test_evaluate_growth_financial_uses_net_income(
    latest_net_income, previous_net_income, expected
):
    latest = _stmt(
        revenue=None,
        net_income=latest_net_income,
        source_report="사업보고서 [금융업: 은행]",
    )
    previous = _stmt(revenue=None, net_income=previous_net_income)

    assert evaluate_growth(latest, previous) == expected


# ── evaluate_stability ────────────────────────────────────────────────────────

def test_evaluate_stability_no_equity_returns_데이터부족():
    latest = _stmt(equity=0, total_liabilities=100)
    assert evaluate_stability(latest) == "데이터 부족"


def test_evaluate_stability_negative_equity_returns_데이터부족():
    latest = _stmt(equity=-1, total_liabilities=100)
    assert evaluate_stability(latest) == "데이터 부족"


def test_evaluate_stability_low_debt_ratio_returns_우수():
    """부채비율 50% — 100% 이하이므로 우수."""
    latest = _stmt(equity=200, total_liabilities=100)
    assert evaluate_stability(latest) == "우수"


def test_evaluate_stability_exact_100_percent_returns_우수():
    """부채비율 정확히 100%는 우수(≤100)."""
    latest = _stmt(equity=100, total_liabilities=100)
    assert evaluate_stability(latest) == "우수"


def test_evaluate_stability_high_debt_ratio_returns_주의():
    """부채비율 200% — 100% 초과이므로 주의."""
    latest = _stmt(equity=100, total_liabilities=200)
    assert evaluate_stability(latest) == "주의"


def test_evaluate_stability_300_percent_returns_주의():
    latest = _stmt(equity=100, total_liabilities=300)
    assert evaluate_stability(latest) == "주의"


def test_evaluate_stability_above_300_percent_returns_위험():
    latest = _stmt(equity=100, total_liabilities=400)
    assert evaluate_stability(latest) == "위험"


@pytest.mark.parametrize(
    ("equity", "total_assets", "expected"),
    [(8, 100, "우수"), (5, 100, "주의"), (3, 100, "위험"), (8, None, "데이터 부족")],
)
def test_evaluate_stability_financial_uses_equity_ratio(equity, total_assets, expected):
    latest = _stmt(
        revenue=None,
        equity=equity,
        total_assets=total_assets,
        source_report="사업보고서 [금융업: 은행]",
    )

    assert evaluate_stability(latest) == expected


# ── evaluate_profitability ────────────────────────────────────────────────────

def test_evaluate_profitability_no_previous_returns_데이터부족():
    latest = _stmt(operating_profit=10, revenue=100)
    assert evaluate_profitability(latest, None) == "데이터 부족"


def test_evaluate_profitability_margin_improved():
    """이익률 10% → 20% 개선."""
    latest = _stmt(operating_profit=20, revenue=100)
    previous = _stmt(operating_profit=10, revenue=100)
    assert evaluate_profitability(latest, previous) == "개선"


def test_evaluate_profitability_margin_declined():
    """이익률 20% → 10% 부진."""
    latest = _stmt(operating_profit=10, revenue=100)
    previous = _stmt(operating_profit=20, revenue=100)
    assert evaluate_profitability(latest, previous) == "부진"


def test_evaluate_profitability_margin_identical_returns_유지():
    """이익률 동일하면 유지."""
    latest = _stmt(operating_profit=20, revenue=100)
    previous = _stmt(operating_profit=20, revenue=100)
    assert evaluate_profitability(latest, previous) == "유지"


def test_evaluate_profitability_float_near_equal_returns_유지():
    """부동소수점 미세 차이(rel_tol=1e-4 이내)는 유지로 처리."""
    # 3/7 과 같은 무한소수를 이용해 표현 오차 유발
    latest = _stmt(operating_profit=3, revenue=7)
    previous = _stmt(operating_profit=3, revenue=7)
    result = evaluate_profitability(latest, previous)
    assert result == "유지"


def test_evaluate_profitability_tiny_change_returns_유지():
    latest = _stmt(operating_profit=105, revenue=1_000)
    previous = _stmt(operating_profit=100, revenue=1_000)
    assert evaluate_profitability(latest, previous) == "유지"


def test_evaluate_profitability_above_1pp_returns_개선():
    latest = _stmt(operating_profit=120, revenue=1_000)
    previous = _stmt(operating_profit=100, revenue=1_000)
    assert evaluate_profitability(latest, previous) == "개선"


def test_evaluate_profitability_zero_revenue_previous():
    """전년 매출이 0이면 이익률 계산 불가 — safe_div가 0.0 반환 → 이익률 동일로 판단."""
    latest = _stmt(operating_profit=10, revenue=0)
    previous = _stmt(operating_profit=0, revenue=0)
    result = evaluate_profitability(latest, previous)
    assert result in ("유지", "개선", "부진")  # 크래시 없음만 검증


@pytest.mark.parametrize(
    ("latest_net_income", "expected"),
    [(12, "개선"), (10.5, "유지"), (8, "부진")],
)
def test_evaluate_profitability_financial_uses_roe_delta(latest_net_income, expected):
    latest = _stmt(
        revenue=None,
        net_income=latest_net_income,
        equity=100,
        source_report="사업보고서 [금융업: 은행]",
    )
    previous = _stmt(revenue=None, net_income=10, equity=100)

    assert evaluate_profitability(latest, previous) == expected


def test_compute_warning_flags_financial_skips_debt_ratio_but_keeps_net_loss():
    latest = _stmt(
        revenue=None,
        net_income=-10,
        equity=100,
        total_liabilities=1_200,
        source_report="사업보고서 [금융업: 은행]",
    )

    flags = compute_warning_flags([latest])

    assert not any("부채비율" in flag for flag in flags)
    assert "당기순손실" in flags


def test_non_financial_evaluation_regression_uses_revenue_and_flags_debt_ratio():
    latest = _stmt(
        revenue=110,
        net_income=90,
        equity=100,
        total_liabilities=1_200,
        source_report="사업보고서",
    )
    previous = _stmt(revenue=100, net_income=100)

    assert evaluate_growth(latest, previous) == "양호"
    assert any("부채비율" in flag for flag in compute_warning_flags([latest]))


# ── compute_metrics ───────────────────────────────────────────────────────────

def test_compute_metrics_all_fields_present():
    latest = _stmt(
        revenue=1_000, operating_profit=100, net_income=80,
        total_assets=2_000, total_liabilities=500, equity=1_500,
    )
    previous = _stmt(revenue=800)
    m = compute_metrics(latest, previous)

    assert m.revenue_growth_rate == pytest.approx(25.0)
    assert m.operating_margin == pytest.approx(10.0)
    assert m.net_margin == pytest.approx(8.0)
    assert m.roe == pytest.approx(round(80 / 1_500 * 100, 2))
    assert m.roa == pytest.approx(round(80 / 2_000 * 100, 2))
    assert m.debt_ratio == pytest.approx(round(500 / 1_500 * 100, 2))


def test_compute_metrics_no_previous_revenue_growth_is_none():
    latest = _stmt(revenue=1_000, operating_profit=100, net_income=80,
                   total_assets=2_000, total_liabilities=500, equity=1_500)
    m = compute_metrics(latest, None)
    assert m.revenue_growth_rate is None


def test_compute_metrics_previous_revenue_zero_growth_is_none():
    """전년 매출 0 — 분모 0 → revenue_growth_rate는 None."""
    latest = _stmt(revenue=1_000, operating_profit=0, net_income=0,
                   total_assets=1, total_liabilities=0, equity=1)
    previous = _stmt(revenue=0)
    m = compute_metrics(latest, previous)
    assert m.revenue_growth_rate is None


def test_compute_metrics_zero_revenue_margins_are_none():
    """매출 0이면 operating_margin, net_margin은 None."""
    latest = _stmt(revenue=0, operating_profit=10, net_income=5,
                   total_assets=100, total_liabilities=30, equity=70)
    m = compute_metrics(latest, None)
    assert m.operating_margin is None
    assert m.net_margin is None


def test_compute_metrics_zero_equity_roe_is_none():
    latest = _stmt(revenue=100, operating_profit=10, net_income=5,
                   total_assets=100, total_liabilities=100, equity=0)
    m = compute_metrics(latest, None)
    assert m.roe is None


def test_compute_metrics_revenue_declined():
    """매출 감소 시 revenue_growth_rate 음수."""
    latest = _stmt(revenue=600, operating_profit=0, net_income=0,
                   total_assets=1, total_liabilities=0, equity=1)
    previous = _stmt(revenue=1_000)
    m = compute_metrics(latest, previous)
    assert m.revenue_growth_rate == pytest.approx(-40.0)


# ── compute_interpretation_points ─────────────────────────────────────────────

def test_interpretation_ncsoft_operating_loss_net_profit():
    latest = _stmt(revenue=1_000, operating_profit=-100, net_income=50)
    metrics = compute_metrics(latest, None)

    points = compute_interpretation_points([latest], metrics)

    assert len(points) == 1
    assert points[0].type == "NET_INCOME_MUCH_HIGHER_THAN_OPERATING_PROFIT"
    assert points[0].severity == "warning"
    assert "영업손실에도" in points[0].message


def test_interpretation_net_over_double_operating():
    latest = _stmt(revenue=1_000, operating_profit=100, net_income=250)
    metrics = compute_metrics(latest, None)

    points = compute_interpretation_points([latest], metrics)

    assert len(points) == 1
    assert points[0].type == "NET_INCOME_MUCH_HIGHER_THAN_OPERATING_PROFIT"


def test_interpretation_low_op_margin_high_net_margin():
    latest = _stmt(revenue=1_000, operating_profit=20, net_income=120)
    metrics = compute_metrics(latest, None)

    points = compute_interpretation_points([latest], metrics)

    assert len(points) == 1
    assert points[0].type == "NET_INCOME_MUCH_HIGHER_THAN_OPERATING_PROFIT"


def test_interpretation_no_duplicate_quality_flag():
    latest = _stmt(revenue=1_000, operating_profit=-10, net_income=120)
    metrics = compute_metrics(latest, None)

    points = compute_interpretation_points([latest], metrics)
    quality_points = [
        p for p in points
        if p.type == "NET_INCOME_MUCH_HIGHER_THAN_OPERATING_PROFIT"
    ]

    assert len(quality_points) == 1


def test_interpretation_revenue_flat_net_surge():
    latest = _stmt(revenue=1_000, operating_profit=100, net_income=200)
    previous = _stmt(revenue=1_100, operating_profit=100, net_income=100)
    metrics = compute_metrics(latest, previous)

    points = compute_interpretation_points([latest, previous], metrics)

    assert any(p.type == "REVENUE_FLAT_NET_INCOME_SURGE" for p in points)


def test_net_income_growth_rate_none_when_prev_loss():
    latest = _stmt(revenue=1_000, operating_profit=100, net_income=200)
    previous = _stmt(revenue=1_100, operating_profit=100, net_income=-100)
    metrics = compute_metrics(latest, previous)

    points = compute_interpretation_points([latest, previous], metrics)

    assert metrics.net_income_growth_rate is None
    assert not any(p.type == "REVENUE_FLAT_NET_INCOME_SURGE" for p in points)


def test_interpretation_operating_profit_turn_to_loss_danger():
    latest = _stmt(revenue=1_000, operating_profit=-10, net_income=-5)
    previous = _stmt(revenue=1_000, operating_profit=100, net_income=50)
    metrics = compute_metrics(latest, previous)

    points = compute_interpretation_points([latest, previous], metrics)

    point = next(p for p in points if p.type == "OPERATING_PROFIT_DETERIORATION")
    assert point.severity == "danger"


def test_interpretation_operating_profit_sharp_drop_warning():
    latest = _stmt(revenue=1_000, operating_profit=40, net_income=20)
    previous = _stmt(revenue=1_000, operating_profit=100, net_income=20)
    metrics = compute_metrics(latest, previous)

    points = compute_interpretation_points([latest, previous], metrics)

    point = next(p for p in points if p.type == "OPERATING_PROFIT_DETERIORATION")
    assert point.severity == "warning"


def test_interpretation_empty_for_financial_none_fields():
    latest = _stmt(revenue=None, operating_profit=None, net_income=100)
    previous = _stmt(revenue=None, operating_profit=None, net_income=50)
    metrics = compute_metrics(latest, previous)

    assert compute_interpretation_points([latest, previous], metrics) == []


def test_interpretation_points_default_empty():
    latest = _stmt(revenue=1_000, operating_profit=100, net_income=50)
    previous = _stmt(revenue=900, operating_profit=90, net_income=45)
    metrics = compute_metrics(latest, previous)

    assert compute_interpretation_points([latest, previous], metrics) == []
