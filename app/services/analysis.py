"""재무 분석 헬퍼 — 평가 함수 및 지표 계산."""
from typing import Optional, List

from app.db.models import FinancialStatement
from app.schemas.finance_schema import InterpretationPoint, Metrics

# 재무 평가 임계치
# 근거: 국내 상장사 평균 매출 성장률을 기준으로 ±5% 이내는 실질적 정체로 간주.
GROWTH_GOOD_THRESHOLD = 0.05
GROWTH_POOR_THRESHOLD = -0.05
# 근거: 일반 제조업 안정 기준 100% 이하, 300% 초과는 통상 자본구조 위험 구간.
DEBT_RATIO_SAFE_MAX = 100.0
DEBT_RATIO_RISK_MIN = 300.0
# 근거: 1%p 이내 영업이익률 변화는 회계 노이즈 수준으로 간주.
PROFITABILITY_DELTA_THRESHOLD = 0.01
# 근거: 국내 은행지주 자기자본/총자산 비율은 통상 7~9% 수준.
# BIS 비율은 위험가중자산 데이터가 없어 산출 불가하므로 총자산 기준으로 근사한다.
FIN_EQUITY_RATIO_GOOD_MIN = 7.0
FIN_EQUITY_RATIO_RISK_MAX = 4.0


def safe_div(numerator, denominator, default: float = 0.0) -> float:
    """None·0 분모 안전 처리. numerator가 None이면 default 반환."""
    if numerator is None or not denominator:
        return default
    return numerator / denominator


def _is_financial_record(record) -> bool:
    """source_report의 금융업 마커로 금융 레코드 여부를 판정한다."""
    return "[금융업" in (getattr(record, "source_report", None) or "")


def evaluate_growth(latest: FinancialStatement, previous: FinancialStatement | None) -> str:
    if not previous:
        return "데이터 부족"
    if _is_financial_record(latest):
        base, prev = latest.net_income, previous.net_income
    else:
        base, prev = latest.revenue, previous.revenue
    if base is None or prev is None or prev == 0:
        return "데이터 부족"
    growth_rate = (base - prev) / abs(prev)
    if growth_rate >= GROWTH_GOOD_THRESHOLD:
        return "양호"
    if growth_rate <= GROWTH_POOR_THRESHOLD:
        return "부진"
    return "보합"


def evaluate_stability(latest: FinancialStatement) -> str:
    if not latest.equity or latest.equity <= 0:
        return "데이터 부족"
    if _is_financial_record(latest):
        if not latest.total_assets:
            return "데이터 부족"
        equity_ratio = latest.equity / latest.total_assets * 100
        if equity_ratio >= FIN_EQUITY_RATIO_GOOD_MIN:
            return "우수"
        if equity_ratio < FIN_EQUITY_RATIO_RISK_MAX:
            return "위험"
        return "주의"
    debt_ratio = safe_div(latest.total_liabilities, latest.equity, float("inf")) * 100
    if debt_ratio <= DEBT_RATIO_SAFE_MAX:
        return "우수"
    if debt_ratio > DEBT_RATIO_RISK_MIN:
        return "위험"
    return "주의"


def evaluate_profitability(latest: FinancialStatement, previous: FinancialStatement | None) -> str:
    """영업이익률 전년 대비 평가. 1%p 이내 변동은 유지로 간주."""
    if not previous:
        return "데이터 부족"
    if _is_financial_record(latest):
        if not latest.equity or latest.equity <= 0 or not previous.equity or previous.equity <= 0:
            return "데이터 부족"
        if latest.net_income is None or previous.net_income is None:
            return "데이터 부족"
        latest_margin = latest.net_income / latest.equity
        previous_margin = previous.net_income / previous.equity
    else:
        if latest.operating_profit is None or latest.revenue is None:
            return "데이터 부족"
        if previous.operating_profit is None or previous.revenue is None:
            return "데이터 부족"
        latest_margin = safe_div(latest.operating_profit, latest.revenue, 0.0)
        previous_margin = safe_div(previous.operating_profit, previous.revenue, 0.0)
    delta = latest_margin - previous_margin
    if abs(delta) <= PROFITABILITY_DELTA_THRESHOLD:
        return "유지"
    return "개선" if delta > 0 else "부진"


def compute_warning_flags(records: list) -> List[str]:
    """재무 이상 지표를 점검하여 경고 플래그 목록을 반환한다."""
    if not records:
        return []
    latest = records[0]
    previous = records[1] if len(records) > 1 else None
    flags: List[str] = []

    # 자본잠식: 자기자본 0 이하
    if latest.equity is not None and latest.equity <= 0:
        flags.append("자본잠식")

    # 부채비율 300% 초과
    if not _is_financial_record(latest) and latest.equity and latest.equity > 0 and latest.total_liabilities:
        debt_ratio = latest.total_liabilities / latest.equity * 100
        if debt_ratio > DEBT_RATIO_RISK_MIN:
            flags.append(f"부채비율 {int(debt_ratio)}% (위험)")

    # 당기순손실
    if latest.net_income is not None and latest.net_income < 0:
        flags.append("당기순손실")

    # 3년 연속 영업손실
    if len(records) >= 3 and all(
        r.operating_profit is not None and r.operating_profit < 0 for r in records[:3]
    ):
        flags.append("3년 연속 영업손실")

    # 전년 대비 매출 30% 이상 급감
    if (
        previous
        and previous.revenue
        and latest.revenue is not None
        and previous.revenue > 0
    ):
        decline = (latest.revenue - previous.revenue) / previous.revenue
        if decline < -0.3:
            flags.append(f"전년 대비 매출 {abs(int(decline * 100))}% 급감")

    return flags


def compute_metrics(
    latest: FinancialStatement,
    previous: Optional[FinancialStatement],
) -> Metrics:
    def pct(num, den) -> Optional[float]:
        if num is None or not den:
            return None
        return round(num / den * 100, 2)

    revenue_growth_rate = None
    if previous and previous.revenue and latest.revenue is not None:
        revenue_growth_rate = round(
            (latest.revenue - previous.revenue) / abs(previous.revenue) * 100, 2
        )

    net_income_growth_rate = None
    if previous and previous.net_income and previous.net_income > 0 and latest.net_income is not None:
        net_income_growth_rate = round(
            (latest.net_income - previous.net_income) / previous.net_income * 100, 2
        )

    return Metrics(
        revenue_growth_rate=revenue_growth_rate,
        net_income_growth_rate=net_income_growth_rate,
        operating_margin=pct(latest.operating_profit, latest.revenue),
        net_margin=pct(latest.net_income, latest.revenue),
        roe=pct(latest.net_income, latest.equity),
        roa=pct(latest.net_income, latest.total_assets),
        debt_ratio=pct(latest.total_liabilities, latest.equity),
    )


def compute_interpretation_points(records: list, metrics: Metrics) -> List[InterpretationPoint]:
    """재무 해석 포인트를 warning_flags와 별도로 계산한다."""
    if not records:
        return []

    latest = records[0]
    previous = records[1] if len(records) > 1 else None
    points: List[InterpretationPoint] = []

    op = latest.operating_profit
    net = latest.net_income
    op_margin = metrics.operating_margin
    net_margin = metrics.net_margin

    net_income_message = (
        "순이익은 좋아 보이지만 영업이익과의 차이가 큽니다. "
        "본업 외 수익이나 일회성 요인이 실적에 반영됐을 수 있어 "
        "영업이익 흐름을 함께 확인해보세요."
    )
    if op is not None and net is not None and op < 0 and net > 0:
        points.append(InterpretationPoint(
            type="NET_INCOME_MUCH_HIGHER_THAN_OPERATING_PROFIT",
            severity="warning",
            title="순이익이 영업이익보다 크게 높습니다",
            message="영업손실에도 당기순이익은 흑자입니다. " + net_income_message,
            metric_refs={
                "operating_profit": op,
                "net_income": net,
                "operating_margin": op_margin,
                "net_margin": net_margin,
            },
        ))
    elif op is not None and net is not None and op > 0 and net > op * 2:
        points.append(InterpretationPoint(
            type="NET_INCOME_MUCH_HIGHER_THAN_OPERATING_PROFIT",
            severity="warning",
            title="순이익이 영업이익보다 크게 높습니다",
            message=net_income_message,
            metric_refs={
                "operating_profit": op,
                "net_income": net,
                "operating_margin": op_margin,
                "net_margin": net_margin,
            },
        ))
    elif op_margin is not None and net_margin is not None and op_margin < 3 and net_margin > 10:
        points.append(InterpretationPoint(
            type="NET_INCOME_MUCH_HIGHER_THAN_OPERATING_PROFIT",
            severity="warning",
            title="순이익이 영업이익보다 크게 높습니다",
            message=net_income_message,
            metric_refs={
                "operating_profit": op,
                "net_income": net,
                "operating_margin": op_margin,
                "net_margin": net_margin,
            },
        ))

    if (
        metrics.revenue_growth_rate is not None
        and metrics.revenue_growth_rate <= 0
        and metrics.net_income_growth_rate is not None
        and metrics.net_income_growth_rate >= 30
    ):
        points.append(InterpretationPoint(
            type="REVENUE_FLAT_NET_INCOME_SURGE",
            severity="warning",
            title="매출은 정체인데 순이익이 크게 늘었습니다",
            message=(
                "매출 성장은 거의 없는데 순이익이 크게 늘었습니다. "
                "일회성 이익·비용 감소·세금 효과 등이 반영됐을 가능성이 있어 "
                "함께 확인해보세요."
            ),
            metric_refs={
                "revenue_growth_rate": metrics.revenue_growth_rate,
                "net_income_growth_rate": metrics.net_income_growth_rate,
                "net_income": net,
            },
        ))

    if previous and previous.operating_profit is not None and op is not None:
        prev_op = previous.operating_profit
        if prev_op > 0 and op < 0:
            points.append(InterpretationPoint(
                type="OPERATING_PROFIT_DETERIORATION",
                severity="danger",
                title="영업이익이 흑자에서 적자로 전환됐습니다",
                message="본업 수익성이 약해진 신호일 수 있어 영업이익 흐름을 함께 확인해보세요.",
                metric_refs={
                    "operating_profit": op,
                    "operating_profit_prev": prev_op,
                },
            ))
        elif prev_op > 0 and op >= 0 and (op - prev_op) / prev_op <= -0.5:
            points.append(InterpretationPoint(
                type="OPERATING_PROFIT_DETERIORATION",
                severity="warning",
                title="영업이익이 전년보다 크게 줄었습니다",
                message="본업 수익성이 약해진 신호일 수 있어 영업이익 흐름을 함께 확인해보세요.",
                metric_refs={
                    "operating_profit": op,
                    "operating_profit_prev": prev_op,
                },
            ))

    return points
