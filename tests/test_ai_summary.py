from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.db.models import FinancialStatement
from app.schemas.finance_schema import InterpretationPoint
from app.services.ai_summary_service import build_prediction_display_text, summarize


class _FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="요약 결과")
                )
            ]
        )


def _fake_client():
    completions = _FakeCompletions()
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=completions
        )
    )
    return client, completions


def _summary_data():
    return {
        "company": "테스트회사",
        "trend": "매출 정체",
        "risk": "부채 안정",
    }


def _seed_financials_with_interpretation_point(db_session, stock_code="005930"):
    rows = [
        (2024, 10_000, -100, 500),
        (2023, 10_000, 100, 100),
        (2022, 9_000, 100, 100),
        (2021, 8_000, 100, 100),
        (2020, 7_000, 100, 100),
    ]
    for fiscal_year, revenue, operating_profit, net_income in rows:
        db_session.add(FinancialStatement(
            company_id=stock_code,
            company_name="Samsung Electronics",
            fiscal_year=fiscal_year,
            revenue=revenue * 100_000_000,
            operating_profit=operating_profit * 100_000_000,
            net_income=net_income * 100_000_000,
            total_assets=30_000 * 100_000_000,
            total_liabilities=10_000 * 100_000_000,
            equity=20_000 * 100_000_000,
            cash=1_000 * 100_000_000,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()


def _seed_prediction_sentence_financials(db_session, stock_code="009999", source_report=None):
    for index in range(5):
        db_session.add(FinancialStatement(
            company_id=stock_code,
            company_name="Sentence Test",
            fiscal_year=2024 - index,
            revenue=10_000 * 100_000_000,
            operating_profit=1_000 * 100_000_000,
            net_income=800 * 100_000_000,
            total_assets=30_000 * 100_000_000,
            total_liabilities=10_000 * 100_000_000,
            equity=20_000 * 100_000_000,
            cash=1_000 * 100_000_000,
            source_report=source_report,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()


def _captured_prompt(completions):
    return completions.kwargs["messages"][0]["content"]


def test_summarize_injects_interpretation_points():
    client, completions = _fake_client()
    point = InterpretationPoint(
        type="NET_INCOME_MUCH_HIGHER_THAN_OPERATING_PROFIT",
        severity="warning",
        title="순이익이 영업이익보다 크게 높습니다",
        message="영업 흐름과 순이익 차이를 함께 확인해야 합니다.",
        metric_refs={"net_income": 100.0},
    )

    with patch("app.services.ai_summary_service._get_client", return_value=client):
        assert summarize(
            _summary_data(),
            interpretation_points=[point],
            base_year=2024,
            forecast_year=2025,
            prediction_display_text="예상 영업이익 약 3,186억 원",
        ) == "요약 결과"

    prompt = _captured_prompt(completions)
    contract_block = (
        "기업:테스트회사\n"
        "기준연도(실적·현재 상태 설명용): 2024년\n"
        "예측 대상 연도(예측 설명용): 2025년\n"
        "예측 표시 문구(그대로 사용): 예상 영업이익 약 3,186억 원\n\n"
        "[연도·예측값 사실성 계약]\n"
        "실적과 현재 상태를 설명할 때는 기준연도를 사용하고, 예측값을 설명할 때는 반드시 예측 대상 연도를 사용한다.\n"
        "예측 문장에는 백엔드가 제공한 예측 표시 문구를 그대로 사용하며 연도·지표명·금액·부호·단위를 바꾸지 않는다.\n"
        "제공되지 않은 연도나 금액을 만들거나 계산·추정·반올림·환산하지 않는다.\n"
        "예측 표시 문구는 하나의 고정 문자열이다. 글자·띄어쓰기·어순을 바꾸거나 문구 내부에 조사를 넣지 말고, 문장에 필요한 조사는 문구 전체 뒤에 붙인다.\n"
        "금액을 언급할 때는 반드시 예측 표시 문구 전체를 그대로 사용한다. 같은 문구를 여러 번 사용할 수 있지만, 금액만 떼어 쓰거나 다른 표현으로 다시 쓰지 말고 각 문장에 예측 대상 연도를 명시한다.\n"
        "맞음: 2026년에는 예상 영업이익 약 12.6조 원을 기록할 것으로 보입니다.\n"
        "틀림: 2026년에는 예상 영업이익이 약 12.6조 원에 이를 것으로 보입니다.\n\n"
        "매출 추세:매출 정체"
    )
    assert contract_block in prompt
    assert prompt.count("맞음: ") == 1
    assert prompt.count("틀림: ") == 1
    assert "예측 영업이익:" not in prompt
    assert "주의해서 해석해야 할 재무 신호" in prompt
    assert point.title in prompt
    assert point.message in prompt
    assert "metric_refs" not in prompt
    assert "좋다/나쁘다" in prompt
    assert "확인이 필요하다" in prompt
    assert "가능성이 있다" in prompt
    assert "기준연도(실적·현재 상태 설명용): 2024년" in prompt
    assert "예측 대상 연도(예측 설명용): 2025년" in prompt
    assert "예측 표시 문구(그대로 사용): 예상 영업이익 약 3,186억 원" in prompt
    assert "약 3,186억 원" in prompt
    assert "318600000000" not in prompt
    assert "연도는 위 기준연도와 예측 대상 연도만 사용하고 다른 연도를 만들지 마라." not in prompt
    assert prompt.rstrip().endswith("위험3개")
    for line in ("3줄 요약", "장점3개", "위험3개", "매출 추세:", "부채 위험:"):
        assert line in prompt


def test_summarize_no_points_keeps_base_prompt():
    client, completions = _fake_client()

    with patch("app.services.ai_summary_service._get_client", return_value=client):
        summarize(
            _summary_data(), base_year=2024, forecast_year=2025,
            prediction_display_text="예상 영업이익 123원",
        )

    prompt = _captured_prompt(completions)
    assert "주의해서 해석해야 할 재무 신호" not in prompt
    contract_block = (
        "기업:테스트회사\n"
        "기준연도(실적·현재 상태 설명용): 2024년\n"
        "예측 대상 연도(예측 설명용): 2025년\n"
        "예측 표시 문구(그대로 사용): 예상 영업이익 123원\n\n"
        "[연도·예측값 사실성 계약]\n"
        "실적과 현재 상태를 설명할 때는 기준연도를 사용하고, 예측값을 설명할 때는 반드시 예측 대상 연도를 사용한다.\n"
        "예측 문장에는 백엔드가 제공한 예측 표시 문구를 그대로 사용하며 연도·지표명·금액·부호·단위를 바꾸지 않는다.\n"
        "제공되지 않은 연도나 금액을 만들거나 계산·추정·반올림·환산하지 않는다.\n"
        "예측 표시 문구는 하나의 고정 문자열이다. 글자·띄어쓰기·어순을 바꾸거나 문구 내부에 조사를 넣지 말고, 문장에 필요한 조사는 문구 전체 뒤에 붙인다.\n"
        "금액을 언급할 때는 반드시 예측 표시 문구 전체를 그대로 사용한다. 같은 문구를 여러 번 사용할 수 있지만, 금액만 떼어 쓰거나 다른 표현으로 다시 쓰지 말고 각 문장에 예측 대상 연도를 명시한다.\n"
        "맞음: 2026년에는 예상 영업이익 약 12.6조 원을 기록할 것으로 보입니다.\n"
        "틀림: 2026년에는 예상 영업이익이 약 12.6조 원에 이를 것으로 보입니다.\n\n"
        "매출 추세:매출 정체"
    )
    assert contract_block in prompt
    assert prompt.count("맞음: ") == 1
    assert prompt.count("틀림: ") == 1
    assert "예측 영업이익:" not in prompt
    assert prompt.rstrip().endswith("위험3개")


def test_build_prediction_display_text_contract():
    cases = [
        (318_600_000_000, False, "예상 영업이익 약 3,186억 원"),
        (-318_600_000_000, False, "예상 영업손실 약 3,186억 원"),
        (35_000_000_000, True, "예상 당기순이익 약 350억 원"),
        (-35_000_000_000, True, "예상 당기순손실 약 350억 원"),
        (0.0, False, "예상 영업이익 0원"),
        (0.0, True, "예상 당기순이익 0원"),
        (12_345.0, False, "예상 영업이익 12,345원"),
        (-12_345.0, False, "예상 영업손실 12,345원"),
        (12_345.5, False, "예상 영업이익 12,346원"),
        (999_960_000_000, False, "예상 영업이익 약 1.0조 원"),
    ]
    for pred, is_financial, expected in cases:
        assert build_prediction_display_text(pred, is_financial) == expected
    assert "약 1.2조 원" in build_prediction_display_text(1_240_000_000_000, False)
    assert "약 1.3조 원" in build_prediction_display_text(1_250_000_000_000, False)


def test_prediction_sentence_invariant_on_success(client, db_session, auth_headers):
    _seed_prediction_sentence_financials(db_session)
    with patch("app.api.v1.finance.get_resolved_code", return_value="009999"), \
         patch("app.api.v1.finance.predict_op_profit", return_value=12345.0), \
         patch("app.api.v1.finance.summarize", return_value="2025년 예상 영업이익 12,345원입니다."):
        data = client.get("/api/v1/finance/summary/ai-analysis?keyword=009999", headers=auth_headers).json()
    assert data["prediction_display_text"] in data["prediction_sentence"]
    assert data["prediction_sentence"] == f"{data['forecast_year']}년 예측 결과는 {data['prediction_display_text']}입니다."


def test_prediction_sentence_invariant_is_independent_of_summary_result(
    client, db_session, auth_headers,
):
    _seed_prediction_sentence_financials(db_session, "009998")
    results = []
    for outcome in ("2025년 예상 영업이익 12,345원입니다.", RuntimeError("failure")):
        with patch("app.api.v1.finance.get_resolved_code", return_value="009998"), \
             patch("app.api.v1.finance.predict_op_profit", return_value=12345.0), \
             patch("app.api.v1.finance.summarize", side_effect=outcome):
            results.append(client.get("/api/v1/finance/summary/ai-analysis?keyword=009998", headers=auth_headers).json())
    assert results[0]["prediction_sentence"] == results[1]["prediction_sentence"]
    assert results[0]["prediction_display_text"] == results[1]["prediction_display_text"]


@pytest.mark.parametrize(
    ("source_report", "predictor", "label"),
    [(None, "predict_op_profit", "영업손실"), ("[금융업] 사업보고서", "predict_net_income", "당기순손실")],
)
def test_prediction_sentence_invariant_keeps_negative_loss_label(
    client, db_session, auth_headers, source_report, predictor, label,
):
    _seed_prediction_sentence_financials(db_session, "009997", source_report)
    with patch("app.api.v1.finance.get_resolved_code", return_value="009997"), \
         patch(f"app.api.v1.finance.{predictor}", return_value=-987.0), \
         patch("app.api.v1.finance.summarize", return_value=f"2025년 예상 {label} 987원입니다."):
        data = client.get("/api/v1/finance/summary/ai-analysis?keyword=009997", headers=auth_headers).json()
    assert label in data["prediction_sentence"]


def test_summarize_rejects_old_positional_prediction_argument():
    with patch("app.services.ai_summary_service._get_client"):
        with pytest.raises(TypeError, match="positional"):
            summarize(
                _summary_data(), 123.0, base_year=2024, forecast_year=2025,
                prediction_display_text="예상 영업이익 123원",
            )


def test_ai_analysis_passes_points_to_summarize(client, db_session, auth_headers):
    stock_code = "005930"
    _seed_financials_with_interpretation_point(db_session, stock_code)

    with patch("app.api.v1.finance.get_resolved_code", return_value=stock_code), \
         patch("app.api.v1.finance.predict_op_profit", return_value=12345.0), \
         patch("app.api.v1.finance.summarize", return_value="요약 결과") as mock_summarize:
        response = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={stock_code}",
            headers=auth_headers,
        )

    assert response.status_code == 200
    points = mock_summarize.call_args.kwargs["interpretation_points"]
    assert points
    assert points[0].title
    assert points[0].message
    assert mock_summarize.call_args.kwargs["base_year"] == 2024
    assert mock_summarize.call_args.kwargs["forecast_year"] == 2025
