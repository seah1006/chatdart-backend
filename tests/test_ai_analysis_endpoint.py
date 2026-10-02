from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.db.models import AIAnalysisCache, FinancialStatement
from app.services.feature_engineering import FEATURE_COLUMNS, FEATURE_COLUMNS_FIN


TEST_STOCK_CODE = "005930"
VALID_SUMMARY = "2025년 예상 영업이익 12,345원입니다."


@pytest.fixture(autouse=True)
def _reset_ai_counter():
    from app.services import ai_analysis_service

    ai_analysis_service._counter_state["date"] = None
    ai_analysis_service._counter_state["count"] = 0
    yield
    ai_analysis_service._counter_state["date"] = None
    ai_analysis_service._counter_state["count"] = 0


def _seed_financials(
    db_session,
    years=5,
    source_report=None,
    stock_code=TEST_STOCK_CODE,
    financial_metrics=False,
    sector_detail=None,
    latest_year=2024,
):
    for idx in range(years):
        year = latest_year - idx
        db_session.add(FinancialStatement(
            company_id=stock_code,
            company_name="Samsung Electronics",
            fiscal_year=year,
            revenue=(10_000 + idx * 1_000) * 100_000_000,
            operating_profit=(1_000 + idx * 100) * 100_000_000,
            net_income=(800 + idx * 100) * 100_000_000,
            total_assets=30_000 * 100_000_000,
            total_liabilities=10_000 * 100_000_000,
            equity=20_000 * 100_000_000,
            cash=1_000 * 100_000_000,
            net_interest_income=((100 + idx * 10) * 100_000_000 if financial_metrics else None),
            loan_loss_provision=((-10 - idx) * 100_000_000 if financial_metrics else None),
            insurance_liability=((500 + idx * 50) * 100_000_000 if financial_metrics else None),
            sector_detail=sector_detail,
            source_report=source_report,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()


def test_ai_analysis_returns_prediction_and_summary(client, db_session, auth_headers):
    _seed_financials(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.predict_op_profit", return_value=12345.0) as mock_predict, \
         patch("app.api.v1.finance.summarize", return_value=VALID_SUMMARY) as mock_summarize:
        resp = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["stock_code"] == TEST_STOCK_CODE
    assert data["company_name"] == "Samsung Electronics"
    assert data["prediction"] == 12345.0
    assert data["base_year"] == 2024
    assert data["forecast_year"] == 2025
    assert data["summary"] == VALID_SUMMARY
    assert data["is_financial"] is False
    assert data["prediction_label"] == "영업이익"
    assert data["prediction_display_text"] == "예상 영업이익 12,345원"
    assert data["prediction_sentence"] == "2025년 예측 결과는 예상 영업이익 12,345원입니다."
    assert "score" not in data
    mock_predict.assert_called_once()
    mock_summarize.assert_called_once()


def test_ai_analysis_passes_30_raw_features_to_create_features(client, db_session, auth_headers):
    _seed_financials(db_session)
    expected_keys = {"company", "trend", "risk"}
    expected_keys.update(
        f"{field}_{idx}"
        for field in ("revenue", "op", "net", "debt", "equity", "cash")
        for idx in range(5)
    )

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch(
             "app.api.v1.finance.create_features",
             return_value={key: 0.0 for key in FEATURE_COLUMNS},
         ) as mock_create_features, \
         patch("app.api.v1.finance.predict_op_profit", return_value=12345.0), \
         patch("app.api.v1.finance.summarize", return_value="요약 텍스트"):
        resp = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    raw_input = mock_create_features.call_args.args[0]
    assert expected_keys.issubset(raw_input.keys())


def test_ai_analysis_returns_422_when_less_than_5_years(client, db_session, auth_headers):
    _seed_financials(db_session, years=3)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        resp = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert resp.status_code == 422
    assert "5년치" in resp.json()["detail"]


def test_ai_analysis_financial_returns_net_income(client, db_session, auth_headers):
    _seed_financials(db_session, source_report="[금융업] 사업보고서")

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch(
             "app.api.v1.finance.create_features_fin",
             return_value={key: 0.0 for key in FEATURE_COLUMNS_FIN},
         ) as mock_create_features_fin, \
         patch("app.api.v1.finance.predict_net_income", return_value=987.0) as mock_predict_net_income, \
         patch("app.api.v1.finance.predict_op_profit") as mock_predict_op_profit, \
         patch("app.api.v1.finance.summarize", return_value="2025년 예상 당기순이익 987원입니다.") as mock_summarize:
        resp = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["prediction"] == 987.0
    assert data["is_financial"] is True
    assert data["prediction_label"] == "당기순이익"
    mock_create_features_fin.assert_called_once()
    mock_predict_net_income.assert_called_once_with(
        {key: 0.0 for key in FEATURE_COLUMNS_FIN},
        base_equity=20_000 * 100_000_000,
    )
    mock_predict_op_profit.assert_not_called()
    mock_summarize.assert_called_once()


def test_ai_analysis_financial_negative_prediction_uses_loss_display(
    client, db_session, auth_headers,
):
    _seed_financials(db_session, source_report="[금융업] 사업보고서")

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.predict_net_income", return_value=-987.0), \
         patch(
             "app.api.v1.finance.summarize",
             return_value="2025년 예상 당기순손실 987원입니다.",
         ):
        response = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert response.status_code == 200
    data = response.json()
    assert data["prediction_display_text"] == "예상 당기순손실 987원"
    assert data["prediction_sentence"] == "2025년 예측 결과는 예상 당기순손실 987원입니다."


def test_ai_analysis_financial_uses_last_two_years(client, db_session, auth_headers):
    _seed_financials(db_session, source_report="[금융업] 사업보고서")

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch(
             "app.api.v1.finance.create_features_fin",
             return_value={key: 0.0 for key in FEATURE_COLUMNS_FIN},
         ) as mock_create_features_fin, \
         patch("app.services.dart_service.DartService.classify_financial_sector", return_value="bank"), \
         patch("app.api.v1.finance.predict_net_income", return_value=987.0), \
         patch("app.api.v1.finance.summarize", return_value="2025년 예상 당기순이익 987원입니다."):
        resp = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    raw_input = mock_create_features_fin.call_args.args[0]
    assert raw_input["ta_0"] == 30_000 * 100_000_000
    assert raw_input["ta_1"] == 30_000 * 100_000_000
    assert raw_input["tl_0"] == 10_000 * 100_000_000
    assert raw_input["tl_1"] == 10_000 * 100_000_000
    assert raw_input["equity_0"] == 20_000 * 100_000_000
    assert raw_input["equity_1"] == 20_000 * 100_000_000
    assert raw_input["net_0"] == 900 * 100_000_000
    assert raw_input["net_1"] == 800 * 100_000_000
    assert raw_input["sector_detail"] == "bank"


def test_ai_analysis_financial_uses_stored_phase90_fields(client, db_session, auth_headers):
    _seed_financials(
        db_session,
        source_report="[금융업] report",
        financial_metrics=True,
        sector_detail="insurance",
    )

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch(
             "app.api.v1.finance.create_features_fin",
             return_value={key: 0.0 for key in FEATURE_COLUMNS_FIN},
         ) as mock_create_features_fin, \
         patch("app.services.dart_service.DartService.classify_financial_sector", return_value="bank"), \
         patch("app.api.v1.finance.predict_net_income", return_value=987.0), \
         patch("app.api.v1.finance.summarize", return_value="summary"):
        resp = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    raw_input = mock_create_features_fin.call_args.args[0]
    assert raw_input["nii_0"] == 110 * 100_000_000
    assert raw_input["nii_1"] == 100 * 100_000_000
    assert raw_input["llp_0"] == -11 * 100_000_000
    assert raw_input["llp_1"] == -10 * 100_000_000
    assert raw_input["ins_liab_0"] == 550 * 100_000_000
    assert raw_input["ins_liab_1"] == 500 * 100_000_000
    assert raw_input["sector_detail"] == "insurance"


def test_records_to_fin_input_degrades_when_sector_unavailable(client, db_session, auth_headers):
    _seed_financials(db_session, source_report="[금융업] 사업보고서")

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch(
             "app.api.v1.finance.create_features_fin",
             return_value={key: 0.0 for key in FEATURE_COLUMNS_FIN},
         ) as mock_create_features_fin, \
         patch("app.services.dart_service.DartService.classify_financial_sector", return_value=""), \
         patch("app.api.v1.finance.predict_net_income", return_value=987.0), \
         patch("app.api.v1.finance.summarize", return_value="2025년 예상 당기순이익 987원입니다."):
        resp = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    raw_input = mock_create_features_fin.call_args.args[0]
    assert raw_input["sector_detail"] == ""


def test_ai_analysis_returns_503_when_model_missing(client, db_session, auth_headers):
    _seed_financials(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.predict_op_profit", side_effect=RuntimeError("모델 없음")):
        resp = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert resp.status_code == 503
    assert "예측 서비스 미준비" in resp.json()["detail"]


def test_ai_analysis_degrades_when_summarize_fails(client, db_session, auth_headers):
    _seed_financials(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.predict_op_profit", return_value=12345.0), \
         patch("app.api.v1.finance.summarize", side_effect=RuntimeError("키 없음")):
        resp = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["prediction"] == 12345.0
    assert data["summary"] is None
    assert data["summary_available"] is False
    assert data["base_year"] == 2024
    assert data["forecast_year"] == 2025
    assert data["is_financial"] is False
    assert data["prediction_label"] == "영업이익"
    assert data["prediction_display_text"] == "예상 영업이익 12,345원"
    assert data["prediction_sentence"] == "2025년 예측 결과는 예상 영업이익 12,345원입니다."
    assert db_session.query(AIAnalysisCache).count() == 0


def test_ai_analysis_degrades_on_generic_summarize_error(client, db_session, auth_headers):
    _seed_financials(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.predict_op_profit", return_value=12345.0), \
         patch("app.api.v1.finance.summarize", side_effect=Exception("openai 500")):
        resp = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["prediction"] == 12345.0
    assert data["summary"] is None
    assert data["summary_available"] is False


def test_summary_year_violation_degrades_without_caching(client, db_session, auth_headers, caplog):
    _seed_financials(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.predict_op_profit", return_value=12345.0), \
         patch("app.api.v1.finance.summarize", return_value="2023년 예상 영업이익"):
        response = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert f"AI 요약 사실성 위반 [{TEST_STOCK_CODE}]" in caplog.text
    data = response.json()
    assert data["summary"] is None
    assert data["summary_available"] is False
    assert data["base_year"] == 2024
    assert data["forecast_year"] == 2025
    assert db_session.query(AIAnalysisCache).count() == 0


def test_summary_year_validation_accepts_only_base_and_forecast_years():
    from app.api.v1.finance import _summary_years_are_valid

    assert _summary_years_are_valid("2025년 예상", 2024, 2025)
    assert not _summary_years_are_valid("2023년 예상", 2024, 2025)
    assert _summary_years_are_valid("연도 없음", 2024, 2025)
    assert _summary_years_are_valid("2024년 실적과 2025년 예상", 2024, 2025)


def test_summary_prediction_violations_contract():
    from app.api.v1.finance import _summary_prediction_violations

    display = "예상 영업이익 약 3,186억 원"
    assert _summary_prediction_violations(
        "2025년 예상 영업이익 약 3,186억 원이 전망됩니다.", 2024, 2025, display,
    ) == []
    assert _summary_prediction_violations(
        "2024년 실적 대비 2025년 예상 영업이익 약 3,186억 원이 전망됩니다.",
        2024, 2025, display,
    ) == []
    assert _summary_prediction_violations(
        "2024년 예상 영업이익 약 3,186억 원입니다.", 2024, 2025, display,
    ) == ["prediction_year"]
    assert _summary_prediction_violations(
        "예상 영업이익 약 3,186억 원입니다.", 2024, 2025, display,
    ) == ["prediction_year"]
    assert _summary_prediction_violations(
        "2025년 영업이익은 약 3,186억 원으로 예상됩니다.", 2024, 2025, display,
    ) == ["display_missing", "extra_amount"]
    assert _summary_prediction_violations(
        "2025년 예상 영업이익 약 3,186억 원, 즉 약 3,200억 원 수준입니다.",
        2024, 2025, display,
    ) == ["extra_amount"]


@pytest.mark.parametrize(
    ("violation_name", "summary"),
    [
        (
            "display_missing",
            "2025년 영업이익 흐름을 함께 확인할 필요가 있습니다.",
        ),
        ("prediction_year", "2024년 예상 영업이익 67,890원입니다."),
        (
            "extra_amount",
            "2025년 예상 영업이익 67,890원, 즉 약 7만 원 수준입니다.",
        ),
    ],
)
def test_summary_prediction_violation_degrades_at_endpoint(
    client, db_session, auth_headers, caplog, monkeypatch, violation_name, summary,
):
    from app.services import ai_analysis_service
    from app.api.v1.finance import _summary_prediction_violations

    display = "예상 영업이익 67,890원"
    assert _summary_prediction_violations(summary, 2024, 2025, display) == [violation_name]
    _seed_financials(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.predict_op_profit", return_value=67890.0), \
         patch("app.api.v1.finance.summarize", return_value=summary):
        response = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert response.status_code == 200
    data = response.json()
    assert data["summary"] is None
    assert data["summary_available"] is False
    assert data["prediction_display_text"] == display
    assert data["prediction_sentence"] == "2025년 예측 결과는 예상 영업이익 67,890원입니다."
    assert db_session.query(AIAnalysisCache).count() == 0
    assert ai_analysis_service._counter_state["count"] == 1
    assert violation_name in caplog.text


def test_summary_year_violation_with_2025_base_degrades(client, db_session, auth_headers, caplog):
    _seed_financials(db_session, latest_year=2025)
    from app.api.v1.finance import _summary_prediction_violations, _summary_years_are_valid

    summary = "2023년 실적 대비 2026년 예상 영업이익 67,890원입니다."
    assert _summary_years_are_valid(summary, 2025, 2026) is False
    assert _summary_prediction_violations(
        summary, 2025, 2026, "예상 영업이익 67,890원"
    ) == []

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.predict_op_profit", return_value=67890.0), \
         patch(
             "app.api.v1.finance.summarize",
             return_value=summary,
         ):
        response = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert "[2023, 2026]" in caplog.text
    data = response.json()
    assert data["summary_available"] is False
    assert data["summary"] is None
    assert data["base_year"] == 2025
    assert data["forecast_year"] == 2026
    assert data["prediction_sentence"].startswith("2026년 예측 결과는 ")
    assert db_session.query(AIAnalysisCache).count() == 0


@pytest.mark.parametrize("prediction", [float("nan"), float("inf"), float("-inf")])
def test_ai_analysis_rejects_non_finite_prediction(
    client, db_session, auth_headers, prediction,
):
    _seed_financials(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.predict_op_profit", return_value=prediction), \
         patch("app.api.v1.finance.summarize") as mock_summarize:
        response = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert response.status_code == 503
    assert "예측 실패" in response.json()["detail"]
    mock_summarize.assert_not_called()


@pytest.mark.parametrize(
    ("summary", "year_valid", "violations"),
    [
        ("2024년 실적 대비 2025년 예상 영업이익 약 3,186억 원입니다.", True, []),
        ("2024년 예상 영업이익 약 3,186억 원입니다.", True, ["prediction_year"]),
        ("예상 영업이익 약 3,186억 원입니다.", True, ["prediction_year"]),
        ("2025년 예상 영업이익 약 3,186억 원입니다.\n2025년 전망은 예상 영업이익 약 3,186억 원입니다.", True, []),
        ("2025년 예상 영업이익 약 3,186억 원입니다.\n2024년 예상 영업이익 약 3,186억 원입니다.", True, ["prediction_year"]),
        ("2025년 예상 영업이익 약 3186억 원입니다.", True, ["display_missing", "extra_amount"]),
        ("2025년 예상 영업이익은 약 3,186억 원입니다.", True, ["display_missing", "extra_amount"]),
        ("2025년 예상 영업이익 약 3,186억 원입니다. 2024년에는 2,000억 원이었습니다.", True, ["extra_amount"]),
        ("2023년 실적 대비 2025년 예상 영업이익 약 3,186억 원입니다.", False, []),
    ],
)
def test_summary_validation_contract_cases(summary, year_valid, violations):
    from app.api.v1.finance import _summary_prediction_violations, _summary_years_are_valid

    display = "예상 영업이익 약 3,186억 원"
    assert _summary_years_are_valid(summary, 2024, 2025) is year_valid
    assert _summary_prediction_violations(summary, 2024, 2025, display) == violations


@pytest.mark.parametrize(
    ("summary", "year_valid", "violations"),
    [
        ("2026년에는 예상 영업이익 약 12.6조 원을 기록할 것으로 보입니다.", True, []),
        ("2026년에는 예상 영업이익이 약 12.6조 원에 이를 것으로 보입니다.", True, ["display_missing", "extra_amount"]),
        ("2026년에는 예상 영업이익 약 12.6조 원을 기록할 것으로 보입니다. 약 12.6조 원은 큰 규모입니다.", True, ["extra_amount"]),
        ("2026년 예상 영업이익 약 12.6조 원입니다.\n2026년에도 예상 영업이익 약 12.6조 원이 전망됩니다.", True, []),
        ("2026년 예상 영업이익 약 12.6조 원입니다. 예상 영업이익 약 12.6조 원이 전망됩니다.", True, ["prediction_year"]),
        ("2026년 예상 영업이익 약 12.6조 원입니다. 2025년 예상 영업이익 약 12.6조 원입니다.", True, ["prediction_year"]),
        ("2026년 예상 영업이익 약 12.6조 원입니다. 2026년 예상 영업이익이 약 12.6조 원입니다.", True, ["extra_amount"]),
    ],
)
def test_summary_validation_ai_particle_cases(summary, year_valid, violations):
    from app.api.v1.finance import _summary_prediction_violations, _summary_years_are_valid
    from app.services.ai_summary_service import build_prediction_display_text

    display = build_prediction_display_text(12_600_000_000_000.0, False)
    assert display == "예상 영업이익 약 12.6조 원"
    assert _summary_years_are_valid(summary, 2025, 2026) is year_valid
    assert _summary_prediction_violations(summary, 2025, 2026, display) == violations


def test_ai_analysis_recovers_after_summary_available(client, db_session, auth_headers):
    _seed_financials(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.predict_op_profit", return_value=12345.0), \
         patch(
             "app.api.v1.finance.summarize",
             side_effect=[RuntimeError("key missing"), VALID_SUMMARY],
         ) as mock_summarize:
        first = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )
        assert first.status_code == 200
        first_data = first.json()
        assert first_data["summary"] is None
        assert first_data["summary_available"] is False
        assert db_session.query(AIAnalysisCache).count() == 0

        second = client.get(
            f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert second.status_code == 200
    second_data = second.json()
    assert second_data["summary"] == VALID_SUMMARY
    assert second_data["summary_available"] is True
    assert db_session.query(AIAnalysisCache).count() == 1
    assert mock_summarize.call_count == 2


def test_ai_analysis_requires_auth(client):
    resp = client.get(f"/api/v1/finance/summary/ai-analysis?keyword={TEST_STOCK_CODE}")

    assert resp.status_code == 401


def test_ai_analysis_rate_limited(client, db_session, auth_headers):
    stock_codes = [f"0059{idx:02d}" for idx in range(21)]
    for stock_code in stock_codes:
        _seed_financials(db_session, stock_code=stock_code)

    with patch("app.api.v1.finance.get_resolved_code", side_effect=lambda keyword: keyword), \
         patch("app.api.v1.finance.predict_op_profit", return_value=12345.0), \
         patch("app.api.v1.finance.summarize", return_value="요약 텍스트"):
        responses = [
            client.get(
                f"/api/v1/finance/summary/ai-analysis?keyword={stock_code}",
                headers=auth_headers,
            )
            for stock_code in stock_codes
        ]

    assert [resp.status_code for resp in responses[:20]] == [200] * 20
    assert responses[20].status_code == 429
