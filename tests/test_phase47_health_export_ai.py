from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import csv
import io

import pytest

from app.db.models import AIAnalysisCache, FinancialStatement


TEST_STOCK_CODE = "005930"


def _seed_summary_records(db_session, stock_code=TEST_STOCK_CODE, latest_year=2024):
    for idx in range(2):
        db_session.add(FinancialStatement(
            company_id=stock_code,
            company_name="삼성전자",
            fiscal_year=latest_year - idx,
            revenue=(3_000 - idx * 100) * 100_000_000,
            operating_profit=(300 - idx * 20) * 100_000_000,
            net_income=(200 - idx * 10) * 100_000_000,
            total_assets=5_000 * 100_000_000,
            total_liabilities=1_500 * 100_000_000,
            equity=3_500 * 100_000_000,
            cost_of_sales=0,
            gross_profit=0,
            sga=0,
            cash=0,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()


def _seed_ai_cache(
    db_session, stock_code=TEST_STOCK_CODE, fiscal_year=2024,
    expires_delta=timedelta(hours=1), summary="2025년 예상 영업이익 약 1억 원입니다.",
):
    cache = AIAnalysisCache(
        stock_code=stock_code,
        fiscal_year=fiscal_year,
        prediction=123_456_789.0,
        summary=summary,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + expires_delta,
    )
    db_session.add(cache)
    db_session.commit()
    return cache


def test_health_includes_ai_fields_when_missing(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "_AI_MODEL_PATH", Path("missing-model.pkl"))
    monkeypatch.setattr(main.settings, "OPENAI_API_KEY", "")

    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ai_model"] == "missing"
    assert body["openai"] == "missing"


def test_health_ai_model_ready_when_file_exists(client, tmp_path, monkeypatch):
    from app import main

    model_path = tmp_path / "model.pkl"
    model_path.write_bytes(b"fake")
    monkeypatch.setattr(main, "_AI_MODEL_PATH", model_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["ai_model"] == "ready"


def test_health_openai_configured_when_key_set(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main.settings, "OPENAI_API_KEY", "sk-test")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["openai"] == "configured"


def test_health_status_unaffected_by_ai_state(client, monkeypatch):
    from app import main

    monkeypatch.setattr(main.DartService, "_global_corp_list", object())
    monkeypatch.setattr(main.DartService, "_init_failed", False)
    monkeypatch.setattr(main, "_AI_MODEL_PATH", Path("missing-model.pkl"))
    monkeypatch.setattr(main.settings, "OPENAI_API_KEY", "")

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["ai_model"] == "missing"
    assert response.json()["openai"] == "missing"


def test_health_ai_model_default_path_points_to_real_model():
    from app import main

    assert main._AI_MODEL_PATH.name == "model_nonfin.pkl"


def test_export_pdf_default_and_include_ai_cache_miss_are_identical(client, db_session, auth_headers):
    _seed_summary_records(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        default_response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자",
            headers=auth_headers,
        )
        include_response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자&include_ai=true",
            headers=auth_headers,
        )

    assert default_response.status_code == 200
    assert include_response.status_code == 200
    assert include_response.content == default_response.content


def test_export_csv_default_and_include_ai_cache_miss_are_identical(client, db_session, auth_headers):
    _seed_summary_records(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        default_response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자&format=csv",
            headers=auth_headers,
        )
        include_response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자&format=csv&include_ai=true",
            headers=auth_headers,
        )

    assert default_response.status_code == 200
    assert include_response.status_code == 200
    assert include_response.content == default_response.content


def test_export_pdf_with_ai_cache_hit_is_larger(client, db_session, auth_headers):
    _seed_summary_records(db_session)
    _seed_ai_cache(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        without_ai = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자",
            headers=auth_headers,
        )
        with_ai = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자&include_ai=true",
            headers=auth_headers,
        )

    assert without_ai.status_code == 200
    assert with_ai.status_code == 200
    assert with_ai.headers["content-type"] == "application/pdf"
    assert len(with_ai.content) > len(without_ai.content)


def test_export_csv_with_ai_cache(client, db_session, auth_headers):
    _seed_summary_records(db_session)
    _seed_ai_cache(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자&format=csv&include_ai=true",
            headers=auth_headers,
        )

    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert ["AI 분석"] in rows
    assert ["다음해 예측 영업이익(원)", "123456789.0"] in rows
    assert ["요약", "2025년 예상 영업이익 약 1억 원입니다."] in rows


def test_export_pdf_omits_invalid_cached_summary(client, db_session, auth_headers):
    _seed_summary_records(db_session)
    _seed_ai_cache(db_session, summary="AI 캐시 요약입니다.")

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        default_response = client.get("/api/v1/finance/summary/export?keyword=삼성전자", headers=auth_headers)
        include_response = client.get("/api/v1/finance/summary/export?keyword=삼성전자&include_ai=true", headers=auth_headers)

    assert include_response.content == default_response.content


@pytest.mark.parametrize(
    "summary",
    [
        pytest.param("AI 캐시 요약입니다.", id="display_missing"),
        pytest.param(
            "2023년 실적 대비 2025년 예상 영업이익 약 1억 원입니다.",
            id="year_invalid",
        ),
    ],
)
def test_export_csv_omits_invalid_cached_summary(client, db_session, auth_headers, summary):
    _seed_summary_records(db_session)
    _seed_ai_cache(db_session, summary=summary)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get("/api/v1/finance/summary/export?keyword=삼성전자&format=csv&include_ai=true", headers=auth_headers)

    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert ["AI 분석"] not in rows


def test_export_csv_include_ai_true_but_cache_miss(client, db_session, auth_headers):
    _seed_summary_records(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자&format=csv&include_ai=true",
            headers=auth_headers,
        )

    assert response.status_code == 200
    content = response.content.decode("utf-8-sig")
    assert "AI 분석" not in content


def test_export_ignores_expired_ai_cache(client, db_session, auth_headers):
    _seed_summary_records(db_session)
    _seed_ai_cache(db_session, expires_delta=timedelta(seconds=-1))

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자&format=csv&include_ai=true",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert "AI 분석" not in response.content.decode("utf-8-sig")


def test_export_does_not_invoke_predict_or_summarize(client, db_session, auth_headers):
    _seed_summary_records(db_session)

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.predict_op_profit") as mock_predict, \
         patch("app.api.v1.finance.summarize") as mock_summarize:
        response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자&include_ai=true",
            headers=auth_headers,
        )

    assert response.status_code == 200
    mock_predict.assert_not_called()
    mock_summarize.assert_not_called()
