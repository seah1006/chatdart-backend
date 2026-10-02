from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.db.models import AIAnalysisCache, FinancialStatement


def _summary(prediction_text: str, year: int = 2025) -> str:
    return f"{year}년 {prediction_text}입니다."


def _seed_financials(db_session, stock_code: str = "005930", latest_year: int = 2024):
    for idx in range(5):
        db_session.add(FinancialStatement(
            company_id=stock_code,
            company_name=f"Company {stock_code}",
            fiscal_year=latest_year - idx,
            revenue=(10_000 + idx * 1_000) * 100_000_000,
            operating_profit=(1_000 + idx * 100) * 100_000_000,
            net_income=(800 + idx * 100) * 100_000_000,
            total_assets=30_000 * 100_000_000,
            total_liabilities=10_000 * 100_000_000,
            equity=20_000 * 100_000_000,
            cash=1_000 * 100_000_000,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


@pytest.fixture(autouse=True)
def _reset_ai_counter():
    from app.services import ai_analysis_service

    ai_analysis_service._counter_state["date"] = None
    ai_analysis_service._counter_state["count"] = 0
    yield
    ai_analysis_service._counter_state["date"] = None
    ai_analysis_service._counter_state["count"] = 0


def test_upsert_cache_uses_configurable_ttl(db_session, monkeypatch):
    from app.core.config import settings
    from app.services import ai_analysis_service

    monkeypatch.setattr(settings, "AI_CACHE_TTL_SECONDS", 259_200)
    now = datetime(2026, 6, 16, 0, 0, tzinfo=timezone.utc)

    ai_analysis_service.upsert_cache(db_session, "005930", 2025, 1.0, "summary", now)

    row = (
        db_session.query(AIAnalysisCache)
        .filter(
            AIAnalysisCache.stock_code == "005930",
            AIAnalysisCache.fiscal_year == 2025,
        )
        .first()
    )
    assert row is not None
    assert _as_utc(row.expires_at) == now + timedelta(seconds=259_200)


def test_ai_analysis_caches_first_call(client, db_session, auth_headers):
    _seed_financials(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value="005930"), \
         patch("app.api.v1.finance.predict_op_profit", return_value=12345.0) as mock_predict, \
         patch("app.api.v1.finance.summarize", return_value=_summary("예상 영업이익 12,345원")) as mock_summarize:
        first = client.get("/api/v1/finance/summary/ai-analysis?keyword=005930", headers=auth_headers)
        second = client.get("/api/v1/finance/summary/ai-analysis?keyword=005930", headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == first.json()
    assert second.json()["is_financial"] is False
    assert second.json()["prediction_label"] == "영업이익"
    assert second.json()["base_year"] == 2024
    assert second.json()["forecast_year"] == 2025
    assert mock_predict.call_count == 2
    mock_summarize.assert_called_once()
    assert db_session.query(AIAnalysisCache).count() == 1


def test_ai_analysis_cache_miss_after_ttl_expired(client, db_session, auth_headers):
    _seed_financials(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value="005930"), \
         patch("app.api.v1.finance.predict_op_profit", side_effect=[12345.0, 67890.0]) as mock_predict, \
         patch("app.api.v1.finance.summarize", side_effect=[
             _summary("예상 영업이익 12,345원"), _summary("예상 영업이익 67,890원")
         ]) as mock_summarize:
        first = client.get("/api/v1/finance/summary/ai-analysis?keyword=005930", headers=auth_headers)
        cache = db_session.query(AIAnalysisCache).first()
        cache.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db_session.commit()
        second = client.get("/api/v1/finance/summary/ai-analysis?keyword=005930", headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["prediction"] == 67890.0
    assert second.json()["summary"] == _summary("예상 영업이익 67,890원")
    assert mock_predict.call_count == 2
    assert mock_summarize.call_count == 2


def test_invalid_cached_summary_is_regenerated(client, db_session, auth_headers):
    from app.services import ai_analysis_service

    _seed_financials(db_session)
    ai_analysis_service.upsert_cache(
        db_session,
        "005930",
        2024,
        1.0,
        "2023년 예상 영업이익",
        datetime.now(timezone.utc),
    )

    with patch("app.api.v1.finance.get_resolved_code", return_value="005930"), \
         patch("app.api.v1.finance.predict_op_profit", return_value=67890.0), \
         patch("app.api.v1.finance.summarize", return_value=_summary("예상 영업이익 67,890원")) as mock_summarize:
        response = client.get("/api/v1/finance/summary/ai-analysis?keyword=005930", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["summary"] == _summary("예상 영업이익 67,890원")
    assert response.json()["base_year"] == 2024
    assert response.json()["forecast_year"] == 2025
    mock_summarize.assert_called_once()
    assert db_session.query(AIAnalysisCache).one().summary == _summary("예상 영업이익 67,890원")


def test_matching_prediction_with_old_display_summary_is_regenerated(
    client, db_session, auth_headers,
):
    from app.services import ai_analysis_service

    _seed_financials(db_session)
    ai_analysis_service.upsert_cache(
        db_session,
        "005930",
        2024,
        67890.0,
        "2025년 영업이익은 약 3,186억 원으로 예상됩니다.",
        datetime.now(timezone.utc),
    )
    new_summary = "2025년 예상 영업이익 67,890원입니다. 새 요약입니다."

    with patch("app.api.v1.finance.get_resolved_code", return_value="005930"), \
         patch("app.api.v1.finance.predict_op_profit", return_value=67890.0), \
         patch("app.api.v1.finance.summarize", return_value=new_summary) as mock_summarize:
        response = client.get(
            "/api/v1/finance/summary/ai-analysis?keyword=005930",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert response.json()["summary"] == new_summary
    mock_summarize.assert_called_once()
    assert db_session.query(AIAnalysisCache).one().summary == new_summary


def test_valid_cache_bypasses_daily_limit(client, db_session, auth_headers, monkeypatch):
    from app.api.v1 import finance as finance_module

    _seed_financials(db_session)
    monkeypatch.setattr(finance_module.settings, "AI_DAILY_LIMIT", 1)
    valid_summary = _summary("예상 영업이익 67,890원")

    with patch("app.api.v1.finance.get_resolved_code", return_value="005930"), \
         patch("app.api.v1.finance.predict_op_profit", return_value=67890.0), \
         patch("app.api.v1.finance.summarize", return_value=valid_summary) as mock_summarize:
        first = client.get(
            "/api/v1/finance/summary/ai-analysis?keyword=005930",
            headers=auth_headers,
        )
        from app.services import ai_analysis_service
        assert ai_analysis_service._counter_state["count"] == finance_module.settings.AI_DAILY_LIMIT
        second = client.get(
            "/api/v1/finance/summary/ai-analysis?keyword=005930",
            headers=auth_headers,
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["summary"] == valid_summary
    mock_summarize.assert_called_once()


def test_cache_prediction_with_subwon_difference_is_reused(client, db_session, auth_headers):
    from app.services import ai_analysis_service

    _seed_financials(db_session)
    ai_analysis_service.upsert_cache(
        db_session, "005930", 2024, 123456789.0,
        _summary("예상 영업이익 약 1억 원"), datetime.now(timezone.utc),
    )
    with patch("app.api.v1.finance.get_resolved_code", return_value="005930"), \
         patch("app.api.v1.finance.predict_op_profit", return_value=123456789.4), \
         patch("app.api.v1.finance.summarize") as mock_summarize:
        response = client.get("/api/v1/finance/summary/ai-analysis?keyword=005930", headers=auth_headers)
    assert response.status_code == 200
    assert response.json()["summary"] == _summary("예상 영업이익 약 1억 원")
    assert mock_summarize.call_count == 0


def test_cache_prediction_with_one_won_difference_is_not_reused(client, db_session, auth_headers):
    from app.services import ai_analysis_service

    _seed_financials(db_session)
    ai_analysis_service.upsert_cache(
        db_session, "005930", 2024, 123456789.0,
        _summary("예상 영업이익 약 1억 원"), datetime.now(timezone.utc),
    )
    with patch("app.api.v1.finance.get_resolved_code", return_value="005930"), \
         patch("app.api.v1.finance.predict_op_profit", return_value=123456790.0), \
         patch("app.api.v1.finance.summarize", return_value=_summary("예상 영업이익 약 1억 원")) as mock_summarize:
        response = client.get("/api/v1/finance/summary/ai-analysis?keyword=005930", headers=auth_headers)
    assert response.status_code == 200
    assert mock_summarize.call_count == 1


def test_canonical_prediction_round_half_up_boundaries():
    from app.services.ai_summary_service import canonical_prediction

    assert canonical_prediction(0.5) == 1
    assert canonical_prediction(-0.5) == -1
    assert canonical_prediction(1.4) == 1


def test_valid_cache_still_requires_prediction_model(client, db_session, auth_headers):
    from app.services import ai_analysis_service

    _seed_financials(db_session)
    ai_analysis_service.upsert_cache(
        db_session,
        "005930",
        2024,
        67890.0,
        _summary("예상 영업이익 67,890원"),
        datetime.now(timezone.utc),
    )

    with patch("app.api.v1.finance.get_resolved_code", return_value="005930"), \
         patch("app.api.v1.finance.predict_op_profit", side_effect=RuntimeError("모델 없음")), \
         patch("app.api.v1.finance.summarize") as mock_summarize:
        response = client.get(
            "/api/v1/finance/summary/ai-analysis?keyword=005930",
            headers=auth_headers,
        )

    assert response.status_code == 503
    assert "예측 서비스 미준비" in response.json()["detail"]
    mock_summarize.assert_not_called()


def test_cached_prediction_mismatch_regenerates_summary(client, db_session, auth_headers):
    from app.services import ai_analysis_service

    _seed_financials(db_session)
    ai_analysis_service.upsert_cache(
        db_session,
        "005930",
        2024,
        1.0,
        _summary("예상 영업이익 67,890원"),
        datetime.now(timezone.utc),
    )

    with patch("app.api.v1.finance.get_resolved_code", return_value="005930"), \
         patch("app.api.v1.finance.predict_op_profit", return_value=67890.0), \
         patch(
             "app.api.v1.finance.summarize",
             return_value="2025년 예상 영업이익 67,890원입니다. 새 요약입니다.",
         ) as mock_summarize:
        response = client.get(
            "/api/v1/finance/summary/ai-analysis?keyword=005930",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert response.json()["summary"] == "2025년 예상 영업이익 67,890원입니다. 새 요약입니다."
    mock_summarize.assert_called_once()


def test_invalid_summary_counts_toward_daily_limit(client, db_session, auth_headers, monkeypatch):
    from app.api.v1 import finance as finance_module

    monkeypatch.setattr(finance_module.settings, "AI_DAILY_LIMIT", 1)
    _seed_financials(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value="005930"), \
         patch("app.api.v1.finance.predict_op_profit", return_value=12345.0), \
         patch("app.api.v1.finance.summarize", return_value="2023년 예상 영업이익") as mock_summarize:
        first = client.get("/api/v1/finance/summary/ai-analysis?keyword=005930", headers=auth_headers)
        second = client.get("/api/v1/finance/summary/ai-analysis?keyword=005930", headers=auth_headers)

    assert first.status_code == 200
    assert first.json()["summary_available"] is False
    assert second.status_code == 503
    assert mock_summarize.call_count == 1


def test_ai_analysis_cache_keyed_by_fiscal_year(client, db_session, auth_headers):
    _seed_financials(db_session, latest_year=2024)

    with patch("app.api.v1.finance.get_resolved_code", return_value="005930"), \
         patch("app.api.v1.finance.predict_op_profit", side_effect=[12345.0, 67890.0]) as mock_predict, \
         patch("app.api.v1.finance.summarize", side_effect=[
             _summary("예상 영업이익 12,345원"), _summary("예상 영업이익 67,890원", 2026)
         ]) as mock_summarize:
        first = client.get("/api/v1/finance/summary/ai-analysis?keyword=005930", headers=auth_headers)
        db_session.add(FinancialStatement(
            company_id="005930",
            company_name="Company 005930",
            fiscal_year=2025,
            revenue=20_000 * 100_000_000,
            operating_profit=2_000 * 100_000_000,
            net_income=1_500 * 100_000_000,
            total_assets=35_000 * 100_000_000,
            total_liabilities=10_000 * 100_000_000,
            equity=25_000 * 100_000_000,
            cash=1_000 * 100_000_000,
            created_at=datetime.now(timezone.utc),
        ))
        db_session.commit()
        second = client.get("/api/v1/finance/summary/ai-analysis?keyword=005930", headers=auth_headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["prediction"] == 67890.0
    assert mock_predict.call_count == 2
    assert mock_summarize.call_count == 2
    assert db_session.query(AIAnalysisCache).count() == 2


def test_ai_analysis_daily_limit_exceeded(client, db_session, auth_headers, monkeypatch):
    from app.api.v1 import finance as finance_module

    monkeypatch.setattr(finance_module.settings, "AI_DAILY_LIMIT", 2)
    stock_codes = ["005930", "000660", "035420"]
    for stock_code in stock_codes:
        _seed_financials(db_session, stock_code=stock_code)

    with patch("app.api.v1.finance.get_resolved_code", side_effect=lambda keyword: keyword), \
         patch("app.api.v1.finance.predict_op_profit", return_value=12345.0) as mock_predict, \
         patch("app.api.v1.finance.summarize", return_value=_summary("예상 영업이익 12,345원")) as mock_summarize:
        responses = [
            client.get(
                f"/api/v1/finance/summary/ai-analysis?keyword={stock_code}",
                headers=auth_headers,
            )
            for stock_code in stock_codes
        ]

    assert [resp.status_code for resp in responses] == [200, 200, 503]
    assert "한도" in responses[2].json()["detail"]
    assert mock_predict.call_count == 3
    assert mock_summarize.call_count == 2


def test_ai_analysis_daily_limit_zero_means_unlimited(client, db_session, auth_headers, monkeypatch):
    from app.api.v1 import finance as finance_module

    monkeypatch.setattr(finance_module.settings, "AI_DAILY_LIMIT", 0)
    stock_codes = [f"0059{idx:02d}" for idx in range(5)]
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

    assert [resp.status_code for resp in responses] == [200, 200, 200, 200, 200]


def test_ai_analysis_cache_not_populated_on_failure(client, db_session, auth_headers):
    _seed_financials(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value="005930"), \
         patch("app.api.v1.finance.predict_op_profit", side_effect=RuntimeError("모델 없음")) as mock_predict:
        first = client.get("/api/v1/finance/summary/ai-analysis?keyword=005930", headers=auth_headers)
        second = client.get("/api/v1/finance/summary/ai-analysis?keyword=005930", headers=auth_headers)

    assert first.status_code == 503
    assert second.status_code == 503
    assert mock_predict.call_count == 2
    assert db_session.query(AIAnalysisCache).count() == 0
