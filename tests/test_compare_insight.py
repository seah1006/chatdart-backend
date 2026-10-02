from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.db.models import FinancialStatement
from app.schemas.finance_schema import CompareInsightResponse, CompanyCompareItem, Metrics
from app.services.ai_summary_service import summarize_compare


class _FakeCompletions:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="compare insight")
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


def _seed_compare_financials(db_session):
    rows = [
        ("005930", "Samsung Electronics", 2024, 200_000_000_000, 20_000_000_000, 10_000_000_000),
        ("005930", "Samsung Electronics", 2023, 100_000_000_000, 8_000_000_000, 5_000_000_000),
        ("000660", "SK hynix", 2024, 150_000_000_000, 15_000_000_000, 7_000_000_000),
        ("000660", "SK hynix", 2023, 120_000_000_000, 9_000_000_000, 4_000_000_000),
    ]
    for code, name, fiscal_year, revenue, operating_profit, net_income in rows:
        db_session.add(FinancialStatement(
            company_id=code,
            company_name=name,
            fiscal_year=fiscal_year,
            revenue=revenue,
            operating_profit=operating_profit,
            net_income=net_income,
            total_assets=200_000_000_000,
            total_liabilities=50_000_000_000,
            equity=150_000_000_000,
            cost_of_sales=0,
            gross_profit=0,
            sga=0,
            cash=0,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()


def _resolve(keyword):
    return "005930" if "Samsung" in keyword else "000660"


def test_summarize_compare_prompt_contains_company_names_and_metrics():
    client, completions = _fake_client()
    companies = [
        {
            "company_name": "Samsung Electronics",
            "fiscal_year": 2024,
            "metrics": {
                "revenue_growth_rate": 100.0,
                "operating_margin": 10.0,
                "net_margin": 5.0,
                "roe": 6.67,
                "roa": 5.0,
                "debt_ratio": 33.33,
            },
        },
        {
            "company_name": "SK hynix",
            "fiscal_year": 2024,
            "metrics": {
                "revenue_growth_rate": 25.0,
                "operating_margin": 10.0,
                "net_margin": 4.67,
                "roe": 4.67,
                "roa": 3.5,
                "debt_ratio": 33.33,
            },
        },
    ]

    with patch("app.services.ai_summary_service._get_client", return_value=client):
        assert summarize_compare(companies) == "compare insight"

    prompt = completions.kwargs["messages"][0]["content"]
    assert "Samsung Electronics" in prompt
    assert "SK hynix" in prompt
    assert "영업이익률 10.0%" in prompt
    assert completions.kwargs["model"] == "gpt-4o-mini"


def test_summarize_compare_propagates_missing_key_runtime_error():
    with patch(
        "app.services.ai_summary_service._get_client",
        side_effect=RuntimeError("missing key"),
    ):
        with pytest.raises(RuntimeError, match="missing key"):
            summarize_compare([
                {
                    "company_name": "Samsung Electronics",
                    "fiscal_year": 2024,
                    "metrics": {},
                }
            ])


def test_compare_ai_insight_success(client, db_session, auth_headers):
    _seed_compare_financials(db_session)

    with patch("app.api.v1.finance.get_resolved_code", side_effect=_resolve), \
         patch("app.api.v1.finance.summarize_compare", return_value="insight") as mock_summarize, \
         patch("app.api.v1.finance.ai_analysis_service.check_daily_limit") as mock_check, \
         patch("app.api.v1.finance.ai_analysis_service.increment_daily_counter") as mock_increment:
        response = client.get(
            "/api/v1/finance/compare/ai-insight?keywords=Samsung&keywords=SK hynix",
            headers=auth_headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["insight"] == "insight"
    assert body["insight_available"] is True
    assert len(body["companies"]) == 2
    mock_check.assert_called_once()
    mock_summarize.assert_called_once()
    mock_increment.assert_called_once()


def test_compare_ai_insight_degrades_when_summarize_fails(client, db_session, auth_headers):
    _seed_compare_financials(db_session)

    with patch("app.api.v1.finance.get_resolved_code", side_effect=_resolve), \
         patch("app.api.v1.finance.summarize_compare", side_effect=RuntimeError("no key")), \
         patch("app.api.v1.finance.ai_analysis_service.check_daily_limit") as mock_check, \
         patch("app.api.v1.finance.ai_analysis_service.increment_daily_counter") as mock_increment:
        response = client.get(
            "/api/v1/finance/compare/ai-insight?keywords=Samsung&keywords=SK hynix",
            headers=auth_headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["insight"] is None
    assert body["insight_available"] is False
    assert len(body["companies"]) == 2
    mock_check.assert_called_once()
    mock_increment.assert_not_called()


def test_compare_ai_insight_one_keyword_returns_422(client, auth_headers):
    response = client.get(
        "/api/v1/finance/compare/ai-insight?keywords=Samsung",
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_compare_ai_insight_missing_data_returns_404(client, auth_headers):
    with patch("app.api.v1.finance.get_resolved_code", return_value="999999"):
        response = client.get(
            "/api/v1/finance/compare/ai-insight?keywords=UnknownA&keywords=UnknownB",
            headers=auth_headers,
        )

    assert response.status_code == 404


def test_compare_insight_response_allows_unavailable_insight():
    item = CompanyCompareItem(
        company_name="Samsung Electronics",
        stock_code="005930",
        fiscal_year=2024,
        sector=None,
        is_financial_sector=False,
        metrics=Metrics(),
        history=[],
    )

    response = CompareInsightResponse(
        companies=[item],
        insight=None,
        insight_available=False,
        compared_at=datetime.now(timezone.utc),
    )

    assert response.insight is None
    assert response.insight_available is False
