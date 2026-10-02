"""
Finance 엔드포인트 통합 테스트 (11개)

- client 픽스처: in-memory DB + DartService 모킹 + TestClient
- get_resolved_code를 패치하여 실제 DART 기업 조회를 우회한다.
"""
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import csv
import io
import pytest
from sqlalchemy.orm import sessionmaker

from app.db.models import AIAnalysisCache, CollectionTask, FinancialStatement, SearchLog

TEST_STOCK_CODE = "005930"


def _add_minimal_financial_statement(db_session, stock_code: str = TEST_STOCK_CODE):
    db_session.add(FinancialStatement(
        company_id=stock_code,
        company_name="Samsung Electronics",
        fiscal_year=2024,
        revenue=1,
        operating_profit=1,
        net_income=1,
        total_assets=1,
        total_liabilities=0,
        equity=1,
        cost_of_sales=0,
        gross_profit=0,
        sga=0,
        cash=0,
        created_at=datetime.now(timezone.utc),
    ))


def test_search_endpoint_without_api_key_returns_401(client):
    """X-API-Key 헤더 없이 요청하면 401이 반환된다 (APIKeyHeader 기본 동작)."""
    response = client.get("/api/v1/finance/search?keyword=삼성")
    assert response.status_code == 401


def test_search_marks_collected_companies(client, auth_headers, db_session):
    """수집 완료된 종목은 is_collected=True."""
    db_session.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="completed"))
    _add_minimal_financial_statement(db_session)
    db_session.commit()

    fake_corp = type("C", (), {
        "corp_name": "Samsung Electronics",
        "stock_code": TEST_STOCK_CODE,
        "induty_code": None,
    })()
    with patch("app.api.v1.finance.DartService.search_companies", return_value=[fake_corp]), \
         patch("app.api.v1.finance.DartService._get_corp_sector", return_value=None):
        response = client.get("/api/v1/finance/search?keyword=Samsung", headers=auth_headers)

    assert response.status_code == 200
    data = response.json()
    assert data[0]["stock_code"] == TEST_STOCK_CODE
    assert data[0]["is_collected"] is True


def test_search_does_not_mark_completed_without_data_collected(client, auth_headers, db_session):
    db_session.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="completed"))
    db_session.commit()

    fake_corp = type("C", (), {
        "corp_name": "Samsung Electronics",
        "stock_code": TEST_STOCK_CODE,
        "induty_code": None,
    })()
    with patch("app.api.v1.finance.DartService.search_companies", return_value=[fake_corp]), \
         patch("app.api.v1.finance.DartService._get_corp_sector", return_value=None):
        response = client.get("/api/v1/finance/search?keyword=Samsung", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()[0]["is_collected"] is False


def test_search_uncollected_default_false(client, auth_headers):
    """CollectionTask가 없는 종목은 is_collected=False."""
    fake_corp = type("C", (), {
        "corp_name": "Test Company",
        "stock_code": "999999",
        "induty_code": None,
    })()
    with patch("app.api.v1.finance.DartService.search_companies", return_value=[fake_corp]), \
         patch("app.api.v1.finance.DartService._get_corp_sector", return_value=None):
        response = client.get("/api/v1/finance/search?keyword=Test", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()[0]["is_collected"] is False


def test_collect_status_no_task_returns_none(client, auth_headers):
    """수집 이력이 없는 종목 조회 시 {"status": "none"}이 반환된다."""
    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/collect/status?keyword=삼성전자",
            headers=auth_headers,
        )
    assert response.status_code == 200
    assert response.json()["status"] == "none"


def test_collect_already_processing_blocks_duplicate(client, auth_headers, db_session):
    """이미 processing 중인 종목에 수집 요청하면 already_processing이 반환된다."""
    task = CollectionTask(stock_code=TEST_STOCK_CODE, status="processing")
    db_session.add(task)
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.post(
            "/api/v1/finance/collect?keyword=삼성전자",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert response.json()["status"] == "already_processing"


def test_collect_retries_failed_task(client, auth_headers, db_session):
    """failed 태스크는 재선점되어 배경 수집을 시작한다."""
    task = CollectionTask(stock_code=TEST_STOCK_CODE, status="failed", message="이전 실패")
    db_session.add(task)
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance._collect_with_email") as collect:
        response = client.post(
            "/api/v1/finance/collect?keyword=삼성전자",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert response.json()["status"] != "already_processing"
    assert response.json()["status"] == "started"
    collect.assert_awaited_once_with(TEST_STOCK_CODE, None)
    db_session.refresh(task)
    assert task.status == "processing"


def test_collect_status_reports_failed_with_message(client, auth_headers, db_session):
    """실패 상태 조회는 프론트 재시도 안내에 필요한 상태와 메시지를 보존한다."""
    task = CollectionTask(stock_code=TEST_STOCK_CODE, status="failed", message="이전 실패")
    db_session.add(task)
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/collect/status?keyword=삼성전자",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert response.json()["message"] == "이전 실패"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/finance/summary/detail?keyword=없는기업",
        "/api/v1/finance/summary?keyword=없는기업",
        "/api/v1/finance/summary/trend?keyword=없는기업",
        "/api/v1/finance/summary/ai-analysis?keyword=없는기업",
        "/api/v1/finance/summary/export?keyword=없는기업",
    ],
    ids=["detail", "summary", "trend", "ai-analysis", "export"],
)
def test_summary_endpoints_return_exact_404_detail_when_no_financial_data(
    client, auth_headers, path
):
    """프론트의 미수집 판정에 쓰는 404 detail 계약을 고정한다."""
    # /compare는 문구가 달라 이 가드에서 제외한다(O122).
    with patch("app.api.v1.finance.get_resolved_code", return_value="999999"):
        response = client.get(path, headers=auth_headers)
    assert response.status_code == 404
    assert response.json()["detail"] == "데이터 수집이 필요합니다."


def test_health_check_returns_200(client):
    """헬스 체크 엔드포인트는 인증 없이 200을 반환한다."""
    response = client.get("/health")
    assert response.status_code == 200


def test_health_check_response_structure(client):
    """헬스 체크 응답에 status, version, corp_list, db 필드가 포함된다."""
    response = client.get("/health")
    body = response.json()
    assert "status" in body
    assert "version" in body
    assert "corp_list" in body
    assert "krx" in body
    assert "db" in body


def test_health_check_reports_krx_degraded(client):
    with patch("app.main.DartService._krx_degraded", True), \
         patch("app.main.DartService._krx_checked", True):
        response = client.get("/health")

    assert response.json()["krx"] == "degraded"


def test_health_check_reports_krx_ok(client):
    with patch("app.main.DartService._krx_degraded", True), \
         patch("app.main.DartService._krx_listed_codes", {"005930"}):
        response = client.get("/health")

    assert response.json()["krx"] == "ok"


@pytest.mark.parametrize(
    ("checked", "init_failed", "expected"),
    [(False, False, "loading"), (False, True, "unknown"), (True, False, "ok")],
)
def test_health_check_reports_krx_initialization_state(client, checked, init_failed, expected):
    with patch("app.main.DartService._krx_listed_codes", None), \
         patch("app.main.DartService._krx_checked", checked), \
         patch("app.main.DartService._init_failed", init_failed), \
         patch("app.main.DartService._krx_degraded", False):
        response = client.get("/health")

    assert response.json()["krx"] == expected


def test_collect_starts_new_task(client, auth_headers):
    """신규 종목 수집 요청 시 task가 생성되고 status=started가 반환된다."""
    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.DartService.fetch_and_process_data"):
        response = client.post(
            "/api/v1/finance/collect?keyword=삼성전자",
            headers=auth_headers,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "started"
    assert body["resolved_code"] == TEST_STOCK_CODE


def test_collect_status_returns_task_status(client, auth_headers, db_session):
    """수집 완료된 태스크 조회 시 completed 상태가 반환된다."""
    task = CollectionTask(stock_code=TEST_STOCK_CODE, status="completed", message="완료")
    db_session.add(task)
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/collect/status?keyword=삼성전자",
            headers=auth_headers,
        )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_summary_returns_200_with_data(client, auth_headers, db_session):
    """재무 데이터가 있을 때 summary는 200과 분석 결과를 반환한다."""
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2024,
        revenue=300_000_000_000, operating_profit=30_000_000_000,
        net_income=20_000_000_000, total_assets=500_000_000_000,
        total_liabilities=150_000_000_000, equity=350_000_000_000,
        cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/summary?keyword=삼성전자",
            headers=auth_headers,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["company_name"] == "삼성전자"
    assert body["growth_status"] == "데이터 부족"   # 전년 데이터 없음
    assert body["stability_status"] == "우수"        # 부채비율 약 43%
    assert body["is_financial_sector"] is False
    assert "history" in body


def test_summary_response_includes_metrics(client, auth_headers, db_session):
    for fiscal_year, revenue in [(2023, 250_000_000_000), (2024, 300_000_000_000)]:
        db_session.add(FinancialStatement(
            company_id=TEST_STOCK_CODE, company_name="Samsung Electronics",
            fiscal_year=fiscal_year,
            revenue=revenue, operating_profit=30_000_000_000,
            net_income=20_000_000_000, total_assets=400_000_000_000,
            total_liabilities=100_000_000_000, equity=300_000_000_000,
            cost_of_sales=0, gross_profit=0, sga=0, cash=0,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/summary?keyword=Samsung",
            headers=auth_headers,
        )

    assert response.status_code == 200
    data = response.json()
    assert "metrics" in data
    assert data["metrics"] is not None
    assert data["metrics"]["operating_margin"] == round(30_000_000_000 / 300_000_000_000 * 100, 2)
    assert data["metrics"]["debt_ratio"] == round(100_000_000_000 / 300_000_000_000 * 100, 2)
    assert data["metrics"]["revenue_growth_rate"] == round(
        (300_000_000_000 - 250_000_000_000) / 250_000_000_000 * 100, 2
    )


def test_summary_history_exposes_financial_fields_scaled_and_none_defaults(
    client,
    auth_headers,
    db_session,
):
    financial_code = "FIN001"
    general_code = "GEN001"
    db_session.add(FinancialStatement(
        company_id=financial_code,
        company_name="KB Financial",
        fiscal_year=2024,
        revenue=100_000_000_000,
        operating_profit=10_000_000_000,
        net_income=8_000_000_000,
        total_assets=500_000_000_000,
        total_liabilities=300_000_000_000,
        equity=200_000_000_000,
        cost_of_sales=0,
        gross_profit=0,
        sga=0,
        cash=1_000_000_000,
        net_interest_income=12_300_000_000,
        loan_loss_provision=4_500_000_000,
        insurance_liability=987_600_000_000,
        sector_detail="bank",
        source_report="[\uae08\uc735\uc5c5] annual",
        created_at=datetime.now(timezone.utc),
    ))
    db_session.add(FinancialStatement(
        company_id=general_code,
        company_name="General Corp",
        fiscal_year=2024,
        revenue=100_000_000_000,
        operating_profit=10_000_000_000,
        net_income=8_000_000_000,
        total_assets=500_000_000_000,
        total_liabilities=300_000_000_000,
        equity=200_000_000_000,
        cost_of_sales=0,
        gross_profit=0,
        sga=0,
        cash=1_000_000_000,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", side_effect=lambda keyword: keyword), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value=None):
        financial_response = client.get(
            f"/api/v1/finance/summary?keyword={financial_code}&unit=%EC%96%B5",
            headers=auth_headers,
        )
        general_response = client.get(
            f"/api/v1/finance/summary?keyword={general_code}&unit=%EC%96%B5",
            headers=auth_headers,
        )

    assert financial_response.status_code == 200
    financial_history = financial_response.json()["history"][0]
    assert financial_response.json()["is_financial_sector"] is True
    assert financial_history["net_interest_income"] == 123
    assert financial_history["loan_loss_provision"] == 45
    assert financial_history["insurance_liability"] == 9876
    assert financial_history["sector_detail"] == "bank"

    assert general_response.status_code == 200
    general_history = general_response.json()["history"][0]
    assert general_history["net_interest_income"] is None
    assert general_history["loan_loss_provision"] is None
    assert general_history["insurance_liability"] is None
    assert general_history["sector_detail"] is None


def test_summary_history_exposes_phase96_fields_scaled_and_null(client, auth_headers, db_session):
    """Phase 96 손익 세부 항목이 history에 단위 변환되어 노출되고, 미수집 값은 null이다."""
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2024,
        revenue=300_000_000_000, operating_profit=30_000_000_000,
        net_income=20_000_000_000, total_assets=500_000_000_000,
        total_liabilities=150_000_000_000, equity=350_000_000_000,
        cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        other_income=1_200_000_000,
        finance_income=3_400_000_000,
        finance_cost=2_100_000_000,
        income_tax_expense=5_600_000_000,
        operating_cash_flow=45_000_000_000,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/summary?keyword=삼성전자&unit=%EC%96%B5",
            headers=auth_headers,
        )

    assert response.status_code == 200
    history = response.json()["history"][0]
    assert history["other_income"] == 12          # 억 단위 변환
    assert history["finance_income"] == 34
    assert history["finance_cost"] == 21
    assert history["income_tax_expense"] == 56
    assert history["operating_cash_flow"] == 450
    # 미수집 필드는 null 유지 — 0으로 뭉개지지 않는다
    assert history["other_expense"] is None
    assert history["income_before_tax"] is None


def test_summary_meta_fields_general(client, auth_headers, db_session):
    """비금융 기업은 industry_type=general, 표기 라벨이 채워진다."""
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2024,
        revenue=300_000_000_000, operating_profit=30_000_000_000,
        net_income=20_000_000_000, total_assets=500_000_000_000,
        total_liabilities=150_000_000_000, equity=350_000_000_000,
        cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/summary?keyword=삼성전자",
            headers=auth_headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["industry_type"] == "general"
    assert body["revenue_label"] == "매출액"
    assert body["operating_profit_label"] == "영업이익"


def test_summary_meta_fields_bank(client, auth_headers, db_session):
    """은행(금융업)은 industry_type=bank, 라벨은 None이다."""
    financial_code = "FIN002"
    db_session.add(FinancialStatement(
        company_id=financial_code, company_name="KB Financial",
        fiscal_year=2024,
        net_income=8_000_000_000, total_assets=500_000_000_000,
        total_liabilities=300_000_000_000, equity=200_000_000_000,
        revenue=0, operating_profit=0, cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        sector_detail="bank",
        source_report="[금융업] annual",
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=financial_code), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value=None):
        response = client.get(
            f"/api/v1/finance/summary?keyword={financial_code}",
            headers=auth_headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert body["is_financial_sector"] is True
    assert body["industry_type"] == "bank"
    assert body["revenue_label"] is None
    assert body["operating_profit_label"] is None


def test_summary_meta_fields_financial_without_sector_detail(client, auth_headers, db_session):
    """금융업인데 sector_detail이 없으면 industry_type=financial로 내려간다."""
    financial_code = "FIN003"
    db_session.add(FinancialStatement(
        company_id=financial_code, company_name="Some Financial",
        fiscal_year=2024,
        net_income=8_000_000_000, total_assets=500_000_000_000,
        total_liabilities=300_000_000_000, equity=200_000_000_000,
        revenue=0, operating_profit=0, cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        source_report="[금융업] annual",
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=financial_code), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value=None):
        response = client.get(
            f"/api/v1/finance/summary?keyword={financial_code}",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert response.json()["industry_type"] == "financial"


def test_summary_text_includes_status(client, auth_headers, db_session):
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="Samsung Electronics",
        fiscal_year=2024, revenue=300_000_000_000,
        operating_profit=30_000_000_000, net_income=20_000_000_000,
        total_assets=400_000_000_000, total_liabilities=100_000_000_000,
        equity=300_000_000_000, cost_of_sales=0, gross_profit=0,
        sga=0, cash=0, created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/summary?keyword=Samsung",
            headers=auth_headers,
        )

    assert response.status_code == 200
    summary_text = response.json()["summary_text"]
    assert "성장성" in summary_text
    assert "안정성" in summary_text
    assert "수익성" in summary_text


# ── /companies 테스트 ─────────────────────────────────────────────────────────

def test_list_companies_empty_when_no_data(client, auth_headers):
    """수집 완료 기업이 없을 때 빈 배열이 반환된다."""
    response = client.get("/api/v1/finance/companies", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_list_companies_pagination(client, auth_headers, db_session):
    """limit/offset 파라미터로 페이지네이션이 동작한다."""
    for i, name in enumerate(["가나다기업", "나다라기업", "다라마기업"]):
        code = f"00000{i+1}"
        db_session.add(CollectionTask(stock_code=code, status="completed"))
        db_session.add(FinancialStatement(
            company_id=code, company_name=name,
            fiscal_year=2024, revenue=1, operating_profit=1, net_income=1,
            total_assets=1, total_liabilities=0, equity=1,
            cost_of_sales=0, gross_profit=0, sga=0, cash=0,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    # 첫 2개
    r1 = client.get("/api/v1/finance/companies?limit=2&offset=0", headers=auth_headers)
    assert r1.status_code == 200
    assert len(r1.json()) == 2

    # 세 번째부터
    r2 = client.get("/api/v1/finance/companies?limit=10&offset=2", headers=auth_headers)
    assert r2.status_code == 200
    assert len(r2.json()) == 1

    # 전체와 첫 페이지 + 두 번째 페이지가 같아야 함
    r_all = client.get("/api/v1/finance/companies?limit=200", headers=auth_headers)
    assert len(r_all.json()) == 3


def test_list_companies_returns_completed_only(client, auth_headers, db_session):
    """completed 상태 기업만 반환되고 processing 기업은 제외된다."""
    db_session.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="completed"))
    db_session.add(CollectionTask(stock_code="000660", status="processing"))
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2024, revenue=1, operating_profit=1, net_income=1,
        total_assets=1, total_liabilities=0, equity=1,
        cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    response = client.get("/api/v1/finance/companies", headers=auth_headers)
    assert response.status_code == 200
    codes = [c["stock_code"] for c in response.json()]
    assert TEST_STOCK_CODE in codes
    assert "000660" not in codes


def test_list_companies_returns_total_count_header(client, auth_headers, db_session):
    """companies 응답은 페이지네이션과 별개로 전체 건수를 헤더에 담는다."""
    for i, name in enumerate(["가나다기업", "나다라기업", "다라마기업"]):
        code = f"10000{i+1}"
        db_session.add(CollectionTask(stock_code=code, status="completed"))
        db_session.add(FinancialStatement(
            company_id=code, company_name=name,
            fiscal_year=2024, revenue=1, operating_profit=1, net_income=1,
            total_assets=1, total_liabilities=0, equity=1,
            cost_of_sales=0, gross_profit=0, sga=0, cash=0,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    response = client.get("/api/v1/finance/companies?limit=2", headers=auth_headers)

    assert response.status_code == 200
    assert response.headers["X-Total-Count"] == "3"
    assert isinstance(response.json(), list)
    assert len(response.json()) == 2


def test_list_companies_sector_filter_total_count(client, auth_headers, db_session):
    """sector 필터가 있으면 필터 적용 후 전체 건수를 헤더에 담는다."""
    companies = [
        ("111111", "IT Company A", "IT"),
        ("222222", "IT Company B", "IT"),
        ("333333", "Bio Company", "BIO"),
    ]
    for code, name, _sector in companies:
        db_session.add(CollectionTask(stock_code=code, status="completed"))
        db_session.add(FinancialStatement(
            company_id=code, company_name=name,
            fiscal_year=2024, revenue=1, operating_profit=1, net_income=1,
            total_assets=1, total_liabilities=0, equity=1,
            cost_of_sales=0, gross_profit=0, sga=0, cash=0,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    sector_by_code = {code: sector for code, _name, sector in companies}
    with patch(
        "app.api.v1.finance.DartService.get_sector_by_stock_code",
        side_effect=lambda code: sector_by_code.get(code),
    ):
        response = client.get(
            "/api/v1/finance/companies?sector=IT&limit=1",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert response.headers["X-Total-Count"] == "2"
    assert isinstance(response.json(), list)
    assert len(response.json()) == 1


# ── /compare 테스트 ───────────────────────────────────────────────────────────

def test_list_companies_sector_filter_returns_matching_only(client, auth_headers, db_session):
    """sector 파라미터가 있으면 해당 섹터 기업만 반환한다."""
    companies = [
        ("111111", "IT Company", "IT"),
        ("222222", "Bio Company", "BIO"),
    ]
    for code, name, _sector in companies:
        db_session.add(CollectionTask(stock_code=code, status="completed"))
        db_session.add(FinancialStatement(
            company_id=code, company_name=name,
            fiscal_year=2024, revenue=1, operating_profit=1, net_income=1,
            total_assets=1, total_liabilities=0, equity=1,
            cost_of_sales=0, gross_profit=0, sga=0, cash=0,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    sector_by_code = {code: sector for code, _name, sector in companies}
    with patch(
        "app.api.v1.finance.DartService.get_sector_by_stock_code",
        side_effect=lambda code: sector_by_code.get(code),
    ):
        response = client.get(
            "/api/v1/finance/companies?sector=IT",
            headers=auth_headers,
        )

    assert response.status_code == 200
    body = response.json()
    assert [item["stock_code"] for item in body] == ["111111"]
    assert body[0]["sector"] == "IT"


def test_list_companies_no_sector_filter_returns_all(client, auth_headers, db_session):
    """sector 파라미터가 없으면 기존처럼 전체 완료 기업을 반환한다."""
    for code, name in [("111111", "IT Company"), ("222222", "Bio Company")]:
        db_session.add(CollectionTask(stock_code=code, status="completed"))
        db_session.add(FinancialStatement(
            company_id=code, company_name=name,
            fiscal_year=2024, revenue=1, operating_profit=1, net_income=1,
            total_assets=1, total_liabilities=0, equity=1,
            cost_of_sales=0, gross_profit=0, sga=0, cash=0,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    response = client.get("/api/v1/finance/companies", headers=auth_headers)

    assert response.status_code == 200
    assert {item["stock_code"] for item in response.json()} == {"111111", "222222"}


def test_compare_missing_data_returns_404(client, auth_headers):
    """수집되지 않은 기업이 포함되면 404가 반환된다."""
    with patch("app.api.v1.finance.get_resolved_code", return_value="999999"):
        response = client.get(
            "/api/v1/finance/compare?keywords=없는기업A&keywords=없는기업B",
            headers=auth_headers,
        )
    assert response.status_code == 404


def test_compare_returns_metrics_for_two_companies(client, auth_headers, db_session):
    """수집 완료된 2개 기업 비교 시 metrics와 history를 포함한 응답이 반환된다."""
    CODE_A, CODE_B = "005930", "000660"
    for code, name in [(CODE_A, "삼성전자"), (CODE_B, "SK하이닉스")]:
        db_session.add(FinancialStatement(
            company_id=code, company_name=name,
            fiscal_year=2024, revenue=100_000_000_000,
            operating_profit=10_000_000_000, net_income=8_000_000_000,
            total_assets=200_000_000_000, total_liabilities=50_000_000_000,
            equity=150_000_000_000, cost_of_sales=0, gross_profit=0,
            sga=0, cash=0, created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    def resolve(keyword):
        return CODE_A if "삼성" in keyword else CODE_B

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve):
        response = client.get(
            "/api/v1/finance/compare?keywords=삼성전자&keywords=SK하이닉스",
            headers=auth_headers,
        )
    assert response.status_code == 200
    body = response.json()
    assert len(body["companies"]) == 2
    assert "compared_at" in body
    for company in body["companies"]:
        assert "metrics" in company
        assert "history" in company
        assert "operating_margin" in company["metrics"]
        assert "is_financial_sector" in company
        assert company["is_financial_sector"] is False


def test_compare_history_includes_ratio_metrics(client, auth_headers, db_session):
    """compare history도 summary와 동일하게 비율 지표를 계산해 반환한다."""
    CODE_A, CODE_B = "005930", "000660"
    rows = [
        (CODE_A, "삼성전자", 2024, 200_000_000_000, 20_000_000_000, 10_000_000_000),
        (CODE_A, "삼성전자", 2023, 100_000_000_000, 8_000_000_000, 5_000_000_000),
        (CODE_B, "SK하이닉스", 2024, 150_000_000_000, 15_000_000_000, 7_000_000_000),
        (CODE_B, "SK하이닉스", 2023, 120_000_000_000, 9_000_000_000, 4_000_000_000),
    ]
    for code, name, year, revenue, operating_profit, net_income in rows:
        db_session.add(FinancialStatement(
            company_id=code, company_name=name, fiscal_year=year,
            revenue=revenue, operating_profit=operating_profit, net_income=net_income,
            total_assets=200_000_000_000, total_liabilities=50_000_000_000,
            equity=150_000_000_000, cost_of_sales=0, gross_profit=0,
            sga=0, cash=0, created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    def resolve(keyword):
        return CODE_A if "삼성" in keyword else CODE_B

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve):
        response = client.get(
            "/api/v1/finance/compare?keywords=삼성전자&keywords=SK하이닉스",
            headers=auth_headers,
        )

    assert response.status_code == 200
    history = response.json()["companies"][0]["history"]
    latest = history[0]
    assert latest["operating_margin"] is not None
    assert latest["net_margin"] is not None
    assert latest["revenue_growth_rate"] is not None
    assert latest["operating_margin"] == 10.0
    assert latest["net_margin"] == 5.0
    assert latest["revenue_growth_rate"] == 100.0


def test_compare_unit_억_scales_history_values(client, auth_headers, db_session):
    """compare unit=억 요청은 history 금액 값을 억 단위로 변환한다."""
    CODE_A, CODE_B = "005930", "000660"
    for code, name in [(CODE_A, "삼성전자"), (CODE_B, "SK하이닉스")]:
        db_session.add(FinancialStatement(
            company_id=code, company_name=name,
            fiscal_year=2024, revenue=100_000_000_000,
            operating_profit=10_000_000_000, net_income=8_000_000_000,
            total_assets=200_000_000_000, total_liabilities=50_000_000_000,
            equity=150_000_000_000, cost_of_sales=0, gross_profit=0,
            sga=0, cash=0, created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    def resolve(keyword):
        return CODE_A if "삼성" in keyword else CODE_B

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve):
        default_response = client.get(
            "/api/v1/finance/compare?keywords=삼성전자&keywords=SK하이닉스",
            headers=auth_headers,
        )
        eok_response = client.get(
            "/api/v1/finance/compare?keywords=삼성전자&keywords=SK하이닉스&unit=억",
            headers=auth_headers,
        )

    assert default_response.status_code == 200
    assert eok_response.status_code == 200
    assert default_response.json()["companies"][0]["history"][0]["revenue"] == 100_000_000_000
    assert eok_response.json()["companies"][0]["history"][0]["revenue"] == 1000


def test_compare_response_includes_sector(client, auth_headers, db_session):
    """compare 응답의 각 기업 항목에 sector 필드가 포함된다."""
    CODE_A, CODE_B = "005930", "000660"
    for code, name in [(CODE_A, "삼성전자"), (CODE_B, "SK하이닉스")]:
        db_session.add(FinancialStatement(
            company_id=code, company_name=name,
            fiscal_year=2024, revenue=100_000_000_000,
            operating_profit=10_000_000_000, net_income=8_000_000_000,
            total_assets=200_000_000_000, total_liabilities=50_000_000_000,
            equity=150_000_000_000, cost_of_sales=0, gross_profit=0,
            sga=0, cash=0, created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    def resolve(keyword):
        return CODE_A if "삼성" in keyword else CODE_B

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve):
        response = client.get(
            "/api/v1/finance/compare?keywords=삼성전자&keywords=SK하이닉스",
            headers=auth_headers,
        )

    assert response.status_code == 200
    companies = response.json()["companies"]
    assert len(companies) == 2
    assert all("sector" in company for company in companies)


def test_compare_sector_value_reflects_dart_service(client, auth_headers, db_session):
    """compare 응답의 sector 값이 DartService.get_sector_by_stock_code 반환값을 사용한다."""
    CODE_A, CODE_B = "005930", "000660"
    for code, name in [(CODE_A, "삼성전자"), (CODE_B, "SK하이닉스")]:
        db_session.add(FinancialStatement(
            company_id=code, company_name=name,
            fiscal_year=2024, revenue=100_000_000_000,
            operating_profit=10_000_000_000, net_income=8_000_000_000,
            total_assets=200_000_000_000, total_liabilities=50_000_000_000,
            equity=150_000_000_000, cost_of_sales=0, gross_profit=0,
            sga=0, cash=0, created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    def resolve(keyword):
        return CODE_A if "삼성" in keyword else CODE_B

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value="전자부품 제조업"):
        response = client.get(
            "/api/v1/finance/compare?keywords=삼성전자&keywords=SK하이닉스",
            headers=auth_headers,
        )

    assert response.status_code == 200
    companies = response.json()["companies"]
    assert len(companies) == 2
    assert all(company["sector"] == "전자부품 제조업" for company in companies)


# ── /collect/refresh 테스트 ──────────────────────────────────────────────────

def test_refresh_starts_recollection(client, auth_headers):
    """수집 이력이 없어도 refresh는 수집을 시작한다."""
    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.DartService.fetch_and_process_data"):
        response = client.post(
            "/api/v1/finance/collect/refresh?keyword=삼성전자",
            headers=auth_headers,
        )
    assert response.status_code == 200
    assert response.json()["status"] == "refreshing"


def test_refresh_blocks_when_processing(client, auth_headers, db_session):
    """수집 중인 종목에 refresh 요청하면 already_processing이 반환된다."""
    db_session.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="processing"))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.post(
            "/api/v1/finance/collect/refresh?keyword=삼성전자",
            headers=auth_headers,
        )
    assert response.json()["status"] == "already_processing"


# ── /summary/export 테스트 ────────────────────────────────────────────────────

def test_export_pdf_returns_pdf_bytes(client, auth_headers, db_session):
    """수집된 데이터가 있으면 PDF 바이트를 반환한다."""
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2024, revenue=300_000_000_000, operating_profit=30_000_000_000,
        net_income=20_000_000_000, total_assets=500_000_000_000,
        total_liabilities=150_000_000_000, equity=350_000_000_000,
        cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자",
            headers=auth_headers,
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content[:4] == b"%PDF"


def test_export_summary_pdf_ignores_unit(client, auth_headers, db_session):
    """summary PDF export는 unit=억 요청이어도 PDF 경로에서 단위 변환을 적용하지 않는다."""
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2024, revenue=100_000_000_000, operating_profit=10_000_000_000,
        net_income=8_000_000_000, total_assets=200_000_000_000,
        total_liabilities=50_000_000_000, equity=150_000_000_000,
        cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자&format=pdf&unit=억",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert "application/pdf" in response.headers["content-type"]


# ── /popular 테스트 ───────────────────────────────────────────────────────────

def test_popular_empty_when_no_logs(client, auth_headers):
    """조회 로그가 없으면 빈 배열이 반환된다."""
    response = client.get("/api/v1/finance/popular", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == []


def test_popular_returns_sorted_by_count(client, auth_headers, db_session):
    """조회 횟수가 많은 기업이 먼저 반환된다."""
    now = datetime.now(timezone.utc)
    for _ in range(3):
        db_session.add(SearchLog(stock_code="005930", company_name="삼성전자", searched_at=now))
    db_session.add(SearchLog(stock_code="035420", company_name="NAVER", searched_at=now))
    db_session.commit()

    response = client.get("/api/v1/finance/popular?period=all", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body[0]["stock_code"] == "005930"
    assert body[0]["search_count"] == 3
    assert body[1]["stock_code"] == "035420"


def test_popular_period_today_filters_old_logs(client, auth_headers, db_session):
    """period=today 이면 오늘 로그만 집계된다."""
    from datetime import timedelta
    old = datetime.now(timezone.utc) - timedelta(days=8)
    now = datetime.now(timezone.utc)
    db_session.add(SearchLog(stock_code="000660", company_name="SK하이닉스", searched_at=old))
    db_session.add(SearchLog(stock_code="005930", company_name="삼성전자", searched_at=now))
    db_session.commit()

    response = client.get("/api/v1/finance/popular?period=today", headers=auth_headers)
    body = response.json()
    codes = [c["stock_code"] for c in body]
    assert "005930" in codes
    assert "000660" not in codes


# ── /search/popular (window 기반) 테스트 ─────────────────────────────────────

def test_search_popular_empty_when_no_logs(client, auth_headers):
    """로그가 없으면 items가 빈 배열이고 window 필드가 포함된다."""
    response = client.get("/api/v1/finance/search/popular", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == []
    assert body["window"] == "realtime"
    assert "generated_at" in body


def test_search_popular_realtime_excludes_old_logs(client, auth_headers, db_session):
    """realtime 윈도우(1시간)는 2시간 전 로그를 제외한다."""
    from datetime import timedelta
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    now = datetime.now(timezone.utc)
    db_session.add(SearchLog(stock_code="000660", company_name="SK하이닉스", searched_at=old))
    db_session.add(SearchLog(stock_code="005930", company_name="삼성전자", searched_at=now))
    db_session.commit()

    response = client.get("/api/v1/finance/search/popular?window=realtime", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    keywords = [item["keyword"] for item in body["items"]]
    assert "삼성전자" in keywords
    assert "SK하이닉스" not in keywords


def test_search_popular_returns_rank_and_count(client, auth_headers, db_session):
    """rank 필드가 1부터 순서대로 부여되고 count가 올바르다."""
    now = datetime.now(timezone.utc)
    for _ in range(5):
        db_session.add(SearchLog(stock_code="005930", company_name="삼성전자", searched_at=now))
    for _ in range(2):
        db_session.add(SearchLog(stock_code="000660", company_name="SK하이닉스", searched_at=now))
    db_session.commit()

    response = client.get("/api/v1/finance/search/popular?window=daily&limit=5", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    items = body["items"]
    assert items[0]["keyword"] == "삼성전자"
    assert items[0]["count"] == 5
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 2
    assert body["window"] == "daily"


def test_search_popular_invalid_window_returns_422(client, auth_headers):
    """유효하지 않은 window 값은 422를 반환한다."""
    response = client.get("/api/v1/finance/search/popular?window=yearly", headers=auth_headers)
    assert response.status_code == 422


# ── 수익성 평가 로직 테스트 ──────────────────────────────────────────────────

def test_summary_profitability_status_부진(client, auth_headers, db_session):
    """영업이익률이 전년 대비 하락하면 profitability_status == '부진'."""
    # 2023: 매출 100, 영업이익 20 → 이익률 20%
    # 2024: 매출 100, 영업이익 10 → 이익률 10% (하락)
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2023, revenue=100, operating_profit=20, net_income=10,
        total_assets=200, total_liabilities=50, equity=150,
        cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2024, revenue=100, operating_profit=10, net_income=8,
        total_assets=200, total_liabilities=50, equity=150,
        cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get("/api/v1/finance/summary?keyword=삼성전자", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["profitability_status"] == "부진"


def test_summary_profitability_status_개선(client, auth_headers, db_session):
    """영업이익률이 전년 대비 상승하면 profitability_status == '개선'."""
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2023, revenue=100, operating_profit=10, net_income=8,
        total_assets=200, total_liabilities=50, equity=150,
        cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2024, revenue=100, operating_profit=20, net_income=15,
        total_assets=200, total_liabilities=50, equity=150,
        cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get("/api/v1/finance/summary?keyword=삼성전자", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["profitability_status"] == "개선"


def test_summary_profitability_status_유지(client, auth_headers, db_session):
    """영업이익률이 동일하면 profitability_status == '유지' (float 비교 오차 방어)."""
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2023, revenue=100, operating_profit=20, net_income=15,
        total_assets=200, total_liabilities=50, equity=150,
        cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2024, revenue=100, operating_profit=20, net_income=15,
        total_assets=200, total_liabilities=50, equity=150,
        cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get("/api/v1/finance/summary?keyword=삼성전자", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["profitability_status"] == "유지"


# ── /compare SearchLog 기록 테스트 ────────────────────────────────────────────

def test_compare_logs_search_for_each_company(client, auth_headers, db_session):
    """/compare 성공 시 기업마다 _write_search_log가 백그라운드로 예약된다."""
    CODE_A, CODE_B = "005930", "000660"
    for code, name in [(CODE_A, "삼성전자"), (CODE_B, "SK하이닉스")]:
        db_session.add(FinancialStatement(
            company_id=code, company_name=name,
            fiscal_year=2024, revenue=100, operating_profit=10, net_income=8,
            total_assets=200, total_liabilities=50, equity=150,
            cost_of_sales=0, gross_profit=0, sga=0, cash=0,
            created_at=datetime.now(timezone.utc),
        ))
    db_session.commit()

    def resolve(keyword):
        return CODE_A if "삼성" in keyword else CODE_B

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve), \
         patch("app.api.v1.finance._write_search_log") as mock_log:
        response = client.get(
            "/api/v1/finance/compare?keywords=삼성전자&keywords=SK하이닉스",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert mock_log.call_count == 2


def test_compare_with_one_keyword_returns_422_count_message(client, auth_headers):
    """비교 기업이 1개면 count 전용 메시지를 반환한다."""
    response = client.get(
        "/api/v1/finance/compare?keywords=삼성전자",
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "비교할 기업을 2개 이상" in response.json()["detail"]


def test_compare_with_six_keywords_returns_422_count_message(client, auth_headers):
    """비교 기업이 6개면 최대 5개 메시지를 반환한다."""
    params = "&".join(f"keywords=기업{i}" for i in range(6))
    response = client.get(
        f"/api/v1/finance/compare?{params}",
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "최대 5개 기업까지 비교" in response.json()["detail"]


def test_compare_with_invalid_keyword_format_returns_422(client, auth_headers):
    """비교 기업 개수는 유효하지만 형식 오류가 있으면 keyword 형식 오류를 반환한다."""
    response = client.get(
        "/api/v1/finance/compare?keywords=삼성전자&keywords=@@@",
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "keyword 형식 오류" in response.json()["detail"]


# ── ServiceNotReadyError → 503 경로 테스트 ───────────────────────────────────

def test_search_returns_503_when_service_not_ready(client, auth_headers):
    """DART 기업 리스트 미로드 상태에서 search 요청 시 503이 반환된다."""
    from app.services.dart_service import ServiceNotReadyError
    with patch(
        "app.api.v1.finance.DartService.search_companies",
        side_effect=ServiceNotReadyError("로딩 중"),
    ):
        response = client.get("/api/v1/finance/search?keyword=삼성", headers=auth_headers)
    assert response.status_code == 503


def test_summary_returns_503_when_service_not_ready(client, auth_headers):
    """DART 기업 리스트 미로드 상태에서 summary 요청 시 503이 반환된다."""
    from app.services.dart_service import ServiceNotReadyError
    with patch(
        "app.services.dart_service.DartService.resolve_stock_code",
        side_effect=ServiceNotReadyError("로딩 중"),
    ):
        response = client.get(
            "/api/v1/finance/summary?keyword=삼성전자", headers=auth_headers
        )
    assert response.status_code == 503


def test_collect_updates_existing_completed_task(client, auth_headers, db_session):
    """완료된 태스크가 있을 때 collect 재요청하면 태스크를 processing으로 업데이트한다."""
    db_session.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="completed"))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.DartService.fetch_and_process_data"):
        response = client.post(
            "/api/v1/finance/collect?keyword=삼성전자", headers=auth_headers
        )
    assert response.status_code == 200
    assert response.json()["status"] == "started"

    task = db_session.query(CollectionTask).filter(
        CollectionTask.stock_code == TEST_STOCK_CODE
    ).first()
    assert task.status == "processing"


def test_collect_skips_recollection_when_completed_with_data(client, auth_headers, db_session):
    db_session.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="completed"))
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE,
        company_name="Samsung Electronics",
        fiscal_year=2024,
        revenue=1,
        operating_profit=1,
        net_income=1,
        total_assets=1,
        total_liabilities=0,
        equity=1,
        cost_of_sales=0,
        gross_profit=0,
        sga=0,
        cash=0,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.DartService.fetch_and_process_data") as mock_fetch:
        response = client.post(
            "/api/v1/finance/collect?keyword=Samsung",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert response.json()["status"] == "already_processing"
    assert response.json()["resolved_code"] == TEST_STOCK_CODE
    mock_fetch.assert_not_called()

    task = db_session.query(CollectionTask).filter(
        CollectionTask.stock_code == TEST_STOCK_CODE
    ).first()
    assert task.status == "completed"


def test_refresh_updates_existing_completed_task(client, auth_headers, db_session):
    """완료된 태스크가 있을 때 refresh 요청하면 태스크를 processing으로 업데이트한다."""
    db_session.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="completed"))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.DartService.fetch_and_process_data"):
        response = client.post(
            "/api/v1/finance/collect/refresh?keyword=삼성전자", headers=auth_headers
        )
    assert response.status_code == 200
    assert response.json()["status"] == "refreshing"

    task = db_session.query(CollectionTask).filter(
        CollectionTask.stock_code == TEST_STOCK_CODE
    ).first()
    assert task.status == "processing"


# ─────────────────────────────────────────────────────────────────────────────

def test_summary_logs_search_on_success(client, auth_headers, db_session):
    """/summary 호출 성공 시 _write_search_log가 백그라운드로 예약된다."""
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2024, revenue=1, operating_profit=1, net_income=1,
        total_assets=1, total_liabilities=0, equity=1,
        cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance._write_search_log") as mock_log:
        client.get("/api/v1/finance/summary?keyword=삼성전자", headers=auth_headers)

    mock_log.assert_called_once_with(TEST_STOCK_CODE, "삼성전자", None)


# ── CompanyNotFoundError → 404 경로 ──────────────────────────────────────────

def test_summary_returns_404_when_company_not_found(client, auth_headers):
    """존재하지 않는 기업명 조회 시 get_resolved_code가 404를 반환한다."""
    from app.services.dart_service import CompanyNotFoundError
    with patch(
        "app.services.dart_service.DartService.resolve_stock_code",
        side_effect=CompanyNotFoundError("없는기업"),
    ):
        response = client.get(
            "/api/v1/finance/summary?keyword=없는기업", headers=auth_headers
        )
    assert response.status_code == 404


def test_collect_returns_404_when_company_not_found(client, auth_headers):
    """존재하지 않는 기업명으로 collect 요청 시 404가 반환된다."""
    from app.services.dart_service import CompanyNotFoundError
    with patch(
        "app.services.dart_service.DartService.resolve_stock_code",
        side_effect=CompanyNotFoundError("없는기업"),
    ):
        response = client.post(
            "/api/v1/finance/collect?keyword=없는기업", headers=auth_headers
        )
    assert response.status_code == 404


# ── Auth 경로 테스트 ──────────────────────────────────────────────────────────

def test_wrong_api_key_returns_403(client):
    """잘못된 API Key로 요청하면 403이 반환된다."""
    response = client.get(
        "/api/v1/finance/summary?keyword=삼성전자",
        headers={"X-API-Key": "wrong-key"},
    )
    assert response.status_code == 403


def test_missing_api_key_returns_401(client):
    """X-API-Key 헤더가 없으면 401이 반환된다."""
    response = client.get("/api/v1/finance/summary?keyword=삼성전자")
    assert response.status_code == 401


# ── collect/status started_at 필드 테스트 ────────────────────────────────────

def test_collect_status_includes_started_at(client, auth_headers, db_session):
    """collect 시작 후 status 조회 시 started_at이 포함된다."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    db_session.add(CollectionTask(
        stock_code=TEST_STOCK_CODE, status="processing", started_at=now
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/collect/status?keyword=삼성전자", headers=auth_headers
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "processing"
    assert body["started_at"] is not None


# ── DartService.initialize() 단위 테스트 ─────────────────────────────────────

def test_initialize_success_sets_corp_list(monkeypatch):
    """initialize() 성공 시 _global_corp_list와 인덱스가 설정된다."""
    from unittest.mock import MagicMock, patch
    from app.services.dart_service import DartService

    mock_corp = MagicMock()
    mock_corp.corp_name = "삼성전자"
    mock_corp.stock_code = "005930"

    mock_corp_list = MagicMock()
    mock_corp_list.corps = [mock_corp]

    with patch("app.services.dart_service.dart.set_api_key"), \
         patch("app.services.dart_service.dart.get_corp_list", return_value=mock_corp_list):
        DartService.initialize()

    assert DartService._global_corp_list is mock_corp_list
    assert DartService._init_failed is False
    assert "삼성전자" in DartService._name_index
    assert DartService._name_index["삼성전자"] == "005930"

    # cleanup
    DartService._global_corp_list = None
    DartService._stock_corps = []
    DartService._name_index = {}


def test_initialize_failure_sets_init_failed(monkeypatch):
    """initialize() 실패 시 _init_failed가 True로 설정된다."""
    from unittest.mock import patch
    from app.services.dart_service import DartService

    with patch("app.services.dart_service.dart.set_api_key"), \
         patch("app.services.dart_service.dart.get_corp_list",
               side_effect=RuntimeError("네트워크 오류")):
        DartService.initialize(_retry_delays=())

    assert DartService._init_failed is True
    assert DartService._global_corp_list is None

    # cleanup
    DartService._init_failed = False


# ── POST /collect/batch 테스트 ────────────────────────────────────────────────

def test_batch_collect_starts_multiple_tasks(client, auth_headers, db_session):
    """2개 기업 일괄 수집 요청 시 두 태스크 모두 started로 반환된다."""
    CODE_A, CODE_B = "005930", "000660"

    def resolve(keyword):
        return CODE_A if "삼성" in keyword else CODE_B

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve), \
         patch("app.api.v1.finance.DartService.fetch_and_process_data"):
        response = client.post(
            "/api/v1/finance/collect/batch?keywords=삼성전자&keywords=SK하이닉스",
            headers=auth_headers,
        )

    assert response.status_code == 200
    results = response.json()["results"]
    assert len(results) == 2
    assert all(r["status"] == "started" for r in results)
    assert {r["resolved_code"] for r in results} == {CODE_A, CODE_B}


def test_batch_collect_with_one_company_returns_422(client, auth_headers):
    """기업 1개만 전달하면 422가 반환된다."""
    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.post(
            "/api/v1/finance/collect/batch?keywords=삼성전자",
            headers=auth_headers,
        )
    assert response.status_code == 422


def test_batch_collect_skips_already_processing(client, auth_headers, db_session):
    """이미 수집 중인 기업은 already_processing으로 건너뛴다."""
    db_session.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="processing"))
    db_session.commit()

    CODE_B = "000660"

    def resolve(keyword):
        return TEST_STOCK_CODE if "삼성" in keyword else CODE_B

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve), \
         patch("app.api.v1.finance.DartService.fetch_and_process_data"):
        response = client.post(
            "/api/v1/finance/collect/batch?keywords=삼성전자&keywords=SK하이닉스",
            headers=auth_headers,
        )

    assert response.status_code == 200
    results = {r["resolved_code"]: r["status"] for r in response.json()["results"]}
    assert results[TEST_STOCK_CODE] == "already_processing"
    assert results[CODE_B] == "started"


def test_batch_collect_skips_completed_with_data(client, auth_headers, db_session):
    db_session.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="completed"))
    _add_minimal_financial_statement(db_session)
    db_session.commit()

    CODE_B = "000660"

    def resolve(keyword):
        return TEST_STOCK_CODE if "Samsung" in keyword else CODE_B

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve), \
         patch("app.api.v1.finance.DartService.fetch_and_process_data") as mock_fetch:
        response = client.post(
            "/api/v1/finance/collect/batch?keywords=Samsung&keywords=SK",
            headers=auth_headers,
        )

    assert response.status_code == 200
    results = {r["resolved_code"]: r["status"] for r in response.json()["results"]}
    assert results[TEST_STOCK_CODE] == "already_processing"
    assert results[CODE_B] == "started"
    mock_fetch.assert_called_once_with(CODE_B)

    task = db_session.query(CollectionTask).filter(
        CollectionTask.stock_code == TEST_STOCK_CODE
    ).first()
    assert task.status == "completed"


def test_batch_collect_duplicate_code_starts_once(client, auth_headers):
    """같은 종목이 한 요청에 중복 포함되면 첫 항목만 started가 된다."""
    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.DartService.fetch_and_process_data"):
        response = client.post(
            "/api/v1/finance/collect/batch?keywords=삼성전자&keywords=삼성전자",
            headers=auth_headers,
        )

    assert response.status_code == 200
    statuses = [item["status"] for item in response.json()["results"]]
    assert statuses == ["started", "already_processing"]


def test_batch_collect_invalid_keyword_returns_error_item(client, auth_headers):
    """배치 수집은 잘못된 keyword 항목만 error로 반환하고 나머지는 계속 처리한다."""
    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.DartService.fetch_and_process_data"):
        response = client.post(
            "/api/v1/finance/collect/batch?keywords=삼성전자&keywords=@@@",
            headers=auth_headers,
        )

    assert response.status_code == 200
    results = response.json()["results"]
    assert results[0]["status"] == "started"
    assert results[1]["status"] == "error"


def test_batch_status_invalid_keyword_returns_error_item(client, auth_headers):
    """배치 상태 조회는 잘못된 keyword 항목만 error로 반환한다."""
    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/collect/status/batch?keywords=삼성전자&keywords=@@@",
            headers=auth_headers,
        )

    assert response.status_code == 200
    items = response.json()
    assert items[0]["status"] == "none"
    assert items[1]["status"] == "error"


def test_keyword_pattern_allows_hyphen_and_ampersand(client, auth_headers):
    """S-Oil, F&F 같은 실제 상장사명 문자를 허용한다."""
    with patch("app.api.v1.finance.DartService.search_companies", return_value=[]):
        resp_hyphen = client.get("/api/v1/finance/search?keyword=S-Oil", headers=auth_headers)
        resp_amp = client.get("/api/v1/finance/search?keyword=F%26F", headers=auth_headers)

    assert resp_hyphen.status_code == 200
    assert resp_amp.status_code == 200


def test_batch_collect_returns_error_for_unknown_company(client, auth_headers):
    """존재하지 않는 기업은 error 항목으로 반환되고 나머지는 정상 처리된다."""
    from fastapi import HTTPException as FastAPIHTTPException

    def resolve(keyword):
        if "없는기업" in keyword:
            raise FastAPIHTTPException(status_code=404, detail="없는기업: 종목코드를 찾을 수 없습니다.")
        return TEST_STOCK_CODE

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve), \
         patch("app.api.v1.finance.DartService.fetch_and_process_data"):
        response = client.post(
            "/api/v1/finance/collect/batch?keywords=삼성전자&keywords=없는기업",
            headers=auth_headers,
        )

    assert response.status_code == 200
    results = {r["keyword"]: r["status"] for r in response.json()["results"]}
    assert results["삼성전자"] == "started"
    assert results["없는기업"] == "error"


# ── DELETE /companies/{stock_code} 테스트 ─────────────────────────────────────

def _make_admin_headers(client, db_session):
    """관리자 JWT Bearer 헤더를 반환하는 헬퍼."""
    from app.core.security import hash_password
    from app.db.models import User

    admin = User(
        email="admin_delete@example.com",
        hashed_password=hash_password("Abcd1234!@"),
        is_admin=True,
        is_active=True,
        is_verified=True,
    )
    db_session.add(admin)
    db_session.commit()

    resp = client.post("/api/v1/auth/login", json={
        "email": "admin_delete@example.com",
        "password": "Abcd1234!@",
    })
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_delete_company_removes_data(client, db_session):
    """수집된 기업 데이터 삭제 후 DB에 레코드가 없어진다."""
    admin_headers = _make_admin_headers(client, db_session)
    db_session.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="completed"))
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2024, revenue=100, operating_profit=10, net_income=8,
        total_assets=200, total_liabilities=50, equity=150,
        cost_of_sales=0, gross_profit=0, sga=0, cash=0,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.add(AIAnalysisCache(
        stock_code=TEST_STOCK_CODE,
        fiscal_year=2024,
        prediction=1.0,
        summary="요약",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    ))
    db_session.commit()

    response = client.delete(
        f"/api/v1/finance/companies/{TEST_STOCK_CODE}", headers=admin_headers
    )
    assert response.status_code == 200
    assert response.json() == {"status": "deleted", "stock_code": TEST_STOCK_CODE}

    assert db_session.query(CollectionTask).filter_by(stock_code=TEST_STOCK_CODE).first() is None
    assert db_session.query(FinancialStatement).filter_by(company_id=TEST_STOCK_CODE).count() == 0
    assert db_session.query(AIAnalysisCache).filter_by(stock_code=TEST_STOCK_CODE).count() == 0


def _seed_collection_cache(db_session, stock_code=TEST_STOCK_CODE):
    db_session.add(CollectionTask(stock_code=stock_code, status="collecting"))
    db_session.add(AIAnalysisCache(
        stock_code=stock_code,
        fiscal_year=2024,
        prediction=1.0,
        summary="오래된 요약",
        expires_at=datetime.now(timezone.utc) + timedelta(days=1),
    ))
    db_session.commit()


def _patch_collection_dependencies(monkeypatch, db_session, prepared_params):
    from app.db import database
    from app.services.dart_service import DartService

    session_factory = sessionmaker(bind=db_session.get_bind())
    corp = SimpleNamespace(corp_name="테스트 기업", stock_code=TEST_STOCK_CODE)
    monkeypatch.setattr(database, "SessionLocal", session_factory)
    monkeypatch.setattr(DartService, "_find_corp", classmethod(lambda cls, _: corp))
    monkeypatch.setattr(DartService, "_is_financial_sector", classmethod(lambda cls, _: False))
    monkeypatch.setattr(DartService, "_latest_fiscal_year", classmethod(lambda cls: 2024))
    monkeypatch.setattr(DartService, "_get_retry_delays", classmethod(lambda cls: (0,)))
    monkeypatch.setattr(DartService, "_collect_financial_data", classmethod(lambda cls, *args: {2024: {}}))
    monkeypatch.setattr(DartService, "_apply_corrections", classmethod(lambda cls, *args: prepared_params))


def test_collection_success_invalidates_ai_cache(db_session, monkeypatch):
    from app.services.dart_service import DartService

    _seed_collection_cache(db_session)
    prepared = [{
        "company_id": TEST_STOCK_CODE,
        "company_name": "테스트 기업",
        "fiscal_year": 2024,
        "revenue": 1,
        "operating_profit": 1,
        "net_income": 1,
        "total_assets": 1,
        "total_liabilities": 0,
        "equity": 1,
        "cash": 0,
        "source_report": "테스트 기업 2024년 사업보고서",
    }]
    _patch_collection_dependencies(monkeypatch, db_session, prepared)

    DartService.fetch_and_process_data(TEST_STOCK_CODE)

    db_session.expire_all()
    assert db_session.query(AIAnalysisCache).filter_by(stock_code=TEST_STOCK_CODE).count() == 0


def test_collection_failure_preserves_ai_cache(db_session, monkeypatch):
    from app.services.dart_service import DartService

    _seed_collection_cache(db_session)
    _patch_collection_dependencies(monkeypatch, db_session, [])

    DartService.fetch_and_process_data(TEST_STOCK_CODE)

    db_session.expire_all()
    assert db_session.query(AIAnalysisCache).filter_by(stock_code=TEST_STOCK_CODE).count() == 1


def test_collection_failure_after_cache_delete_rolls_back_all_changes(db_session, monkeypatch):
    from app.services.dart_service import DartService

    _seed_collection_cache(db_session)
    _add_minimal_financial_statement(db_session)
    db_session.commit()
    bad_params = [{"company_id": TEST_STOCK_CODE, "not_a_financial_statement_field": True}]
    _patch_collection_dependencies(monkeypatch, db_session, bad_params)

    DartService.fetch_and_process_data(TEST_STOCK_CODE)

    db_session.expire_all()
    assert db_session.query(AIAnalysisCache).filter_by(stock_code=TEST_STOCK_CODE).count() == 1
    assert db_session.query(FinancialStatement).filter_by(company_id=TEST_STOCK_CODE).count() == 1


def test_delete_company_not_found_returns_404(client, db_session):
    """수집 이력이 없는 종목 삭제 시 404가 반환된다."""
    admin_headers = _make_admin_headers(client, db_session)
    response = client.delete(
        "/api/v1/finance/companies/999999", headers=admin_headers
    )
    assert response.status_code == 404


def test_delete_company_invalid_stock_code_returns_422(client, db_session):
    """6자리 숫자가 아닌 stock_code는 422를 반환한다."""
    admin_headers = _make_admin_headers(client, db_session)
    response = client.delete(
        "/api/v1/finance/companies/not-a-code", headers=admin_headers
    )
    assert response.status_code == 422


def test_delete_company_requires_admin(client, db_session):
    """일반 사용자(is_admin=False)가 DELETE 요청 시 403을 반환한다."""
    from app.core.security import hash_password
    from app.db.models import User

    user = User(
        email="normal_delete@example.com",
        hashed_password=hash_password("Abcd1234!@"),
        is_admin=False,
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()

    resp = client.post("/api/v1/auth/login", json={
        "email": "normal_delete@example.com",
        "password": "Abcd1234!@",
    })
    token = resp.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    response = client.delete(
        f"/api/v1/finance/companies/{TEST_STOCK_CODE}", headers=headers
    )
    assert response.status_code == 403


def test_delete_company_api_key_alone_rejected(client, auth_headers):
    """X-API-Key 단독 요청은 401(인증 없음)을 반환한다."""
    response = client.delete(
        f"/api/v1/finance/companies/{TEST_STOCK_CODE}", headers=auth_headers
    )
    assert response.status_code == 401


# ── POST /admin/dart/reinitialize 테스트 ──────────────────────────────────────

def test_reinitialize_dart_requires_auth(client):
    """인증 없이 호출하면 401."""
    response = client.post("/api/v1/admin/dart/reinitialize")
    assert response.status_code == 401


def test_reinitialize_dart_requires_admin(client, db_session):
    """일반 사용자(is_admin=False) 호출 시 403."""
    from app.core.security import hash_password
    from app.db.models import User

    user = User(
        email="normal_reinit@example.com",
        hashed_password=hash_password("Abcd1234!@"),
        is_admin=False,
        is_active=True,
        is_verified=True,
    )
    db_session.add(user)
    db_session.commit()

    resp = client.post("/api/v1/auth/login", json={
        "email": "normal_reinit@example.com",
        "password": "Abcd1234!@",
    })
    token = resp.json()["access_token"]

    response = client.post(
        "/api/v1/admin/dart/reinitialize",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_reinitialize_dart_admin_returns_status(client, db_session):
    """관리자 호출 시 200과 상태 문자열을 반환한다."""
    from unittest.mock import MagicMock
    from app.services.dart_service import DartService

    admin_headers = _make_admin_headers(client, db_session)
    # 로딩 완료 상태로 두면 스레드를 생성하지 않아 결정적으로 already_ready를 반환한다.
    with patch.object(DartService, "_global_corp_list", MagicMock()):
        response = client.post(
            "/api/v1/admin/dart/reinitialize", headers=admin_headers
        )
    assert response.status_code == 200
    assert response.json()["status"] == "already_ready"


# ── GET /compare/export 테스트 ────────────────────────────────────────────────

def test_export_compare_pdf_returns_pdf_bytes(client, auth_headers, db_session):
    """2개 기업 비교 PDF 요청 시 application/pdf가 반환된다."""
    CODE_A, CODE_B = "005930", "000660"
    _add_fs(db_session, CODE_A, "삼성전자", 2024)
    _add_fs(db_session, CODE_B, "SK하이닉스", 2024)
    db_session.commit()

    def resolve(keyword):
        return CODE_A if "삼성" in keyword else CODE_B

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve):
        response = client.get(
            "/api/v1/finance/compare/export?keywords=삼성전자&keywords=SK하이닉스",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert len(response.content) > 0


def test_export_compare_pdf_ignores_unit(client, auth_headers, db_session):
    """compare PDF export는 unit=억 요청이어도 PDF 경로에서 단위 변환을 적용하지 않는다."""
    CODE_A, CODE_B = "005930", "000660"
    _add_fs(db_session, CODE_A, "삼성전자", 2024, revenue=100_000_000_000)
    _add_fs(db_session, CODE_B, "SK하이닉스", 2024, revenue=100_000_000_000)
    db_session.commit()

    def resolve(keyword):
        return CODE_A if "삼성" in keyword else CODE_B

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve):
        response = client.get(
            "/api/v1/finance/compare/export?format=pdf&unit=억&keywords=삼성전자&keywords=SK하이닉스",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert "application/pdf" in response.headers["content-type"]


def test_export_compare_pdf_missing_data_returns_404(client, auth_headers, db_session):
    """수집되지 않은 기업이 포함된 경우 404가 반환된다."""
    _add_fs(db_session, TEST_STOCK_CODE, "삼성전자", 2024)
    db_session.commit()

    def resolve(keyword):
        return TEST_STOCK_CODE if "삼성" in keyword else "999999"

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve):
        response = client.get(
            "/api/v1/finance/compare/export?keywords=삼성전자&keywords=없는기업",
            headers=auth_headers,
        )
    assert response.status_code == 404


# ── Feature 1: 수집 상태 일괄 조회 (/collect/status/batch) ───────────────────

def test_batch_status_returns_none_for_uncollected(client, auth_headers):
    """수집 이력 없는 종목은 status=none을 반환한다."""
    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/collect/status/batch?keywords=삼성전자",
            headers=auth_headers,
        )
    assert response.status_code == 200
    items = response.json()
    assert items[0]["status"] == "none"
    assert items[0]["resolved_code"] == TEST_STOCK_CODE


def test_batch_status_returns_completed(client, auth_headers, db_session):
    """수집 완료된 종목은 status=completed를 반환한다."""
    db_session.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="completed"))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/collect/status/batch?keywords=삼성전자",
            headers=auth_headers,
        )
    assert response.status_code == 200
    assert response.json()[0]["status"] == "completed"


def test_batch_status_error_for_unknown_company(client, auth_headers):
    """존재하지 않는 기업은 status=error를 반환하고 전체 요청은 200이다."""
    from fastapi import HTTPException
    with patch(
        "app.api.v1.finance.get_resolved_code",
        side_effect=HTTPException(status_code=404, detail="기업 없음"),
    ):
        response = client.get(
            "/api/v1/finance/collect/status/batch?keywords=없는기업",
            headers=auth_headers,
        )
    assert response.status_code == 200
    assert response.json()[0]["status"] == "error"


def test_batch_status_over_limit_returns_422(client, auth_headers):
    """11개 초과 요청은 422를 반환한다."""
    params = "&".join(f"keywords=기업{i}" for i in range(11))
    response = client.get(
        f"/api/v1/finance/collect/status/batch?{params}",
        headers=auth_headers,
    )
    assert response.status_code == 422


# ── Feature 2: warning_flags (/summary) ─────────────────────────────────────

def test_summary_includes_warning_flags_field(client, auth_headers, db_session):
    """summary 응답에 warning_flags 배열이 포함된다."""
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2024, revenue=1000, operating_profit=100, net_income=80,
        total_assets=2000, total_liabilities=500, equity=1500,
        cost_of_sales=0, gross_profit=0, sga=0, cash=100,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get("/api/v1/finance/summary?keyword=삼성전자", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert "warning_flags" in body
    assert isinstance(body["warning_flags"], list)


def test_summary_warning_flag_net_loss(client, auth_headers, db_session):
    """당기순손실 기업에는 '당기순손실' 플래그가 포함된다."""
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="손실기업",
        fiscal_year=2024, revenue=1000, operating_profit=-50, net_income=-100,
        total_assets=2000, total_liabilities=500, equity=1500,
        cost_of_sales=0, gross_profit=0, sga=0, cash=100,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get("/api/v1/finance/summary?keyword=손실기업", headers=auth_headers)

    assert "당기순손실" in response.json()["warning_flags"]


# ── Feature 3: CSV 내보내기 ──────────────────────────────────────────────────

def _add_fs(session, code, name, year, **kw):
    defaults = dict(
        revenue=1_000_000, operating_profit=100_000, net_income=80_000,
        total_assets=2_000_000, total_liabilities=500_000, equity=1_500_000,
        cost_of_sales=0, gross_profit=0, sga=0, cash=200_000,
    )
    defaults.update(kw)
    session.add(FinancialStatement(
        company_id=code, company_name=name, fiscal_year=year,
        created_at=datetime.now(timezone.utc), **defaults,
    ))


def test_summary_sector_avg_included_when_peers_exist(client, auth_headers, db_session):
    """동일 섹터 수집 완료 기업이 있으면 summary 응답에 sector_avg가 포함된다."""
    target_code = TEST_STOCK_CODE
    peer_code = "000660"
    db_session.add(CollectionTask(stock_code=target_code, status="completed"))
    db_session.add(CollectionTask(stock_code=peer_code, status="completed"))
    _add_fs(db_session, target_code, "Samsung", 2024, revenue=2_000_000, operating_profit=200_000)
    _add_fs(db_session, peer_code, "Peer", 2024, revenue=1_000_000, operating_profit=150_000)
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=target_code), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value="IT"):
        response = client.get(
            "/api/v1/finance/summary?keyword=Samsung",
            headers=auth_headers,
        )

    assert response.status_code == 200
    sector_avg = response.json()["sector_avg"]
    assert sector_avg is not None
    assert isinstance(sector_avg["operating_margin"], float)


def test_summary_sector_avg_none_when_no_peers(client, auth_headers, db_session):
    """동일 섹터 peer가 없으면 sector_avg는 null이다."""
    db_session.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="completed"))
    _add_fs(db_session, TEST_STOCK_CODE, "Samsung", 2024)
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value="IT"):
        response = client.get(
            "/api/v1/finance/summary?keyword=Samsung",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert response.json()["sector_avg"] is None


def test_compute_avg_metrics_excludes_negative_equity():
    """음수 equity 레코드는 roe, debt_ratio 집계에서 제외한다."""
    from app.api.v1.finance import _compute_avg_metrics
    from app.db.models import FinancialStatement
    from datetime import datetime, timezone

    def make_fs(**kw):
        defaults = dict(
            revenue=1_000_000, operating_profit=100_000, net_income=80_000,
            total_assets=2_000_000, total_liabilities=500_000, equity=1_000_000,
            cost_of_sales=0, gross_profit=0, sga=0, cash=0,
            created_at=datetime.now(timezone.utc),
        )
        defaults.update(kw)
        return FinancialStatement(**defaults)

    normal = make_fs(equity=1_000_000, net_income=80_000, total_liabilities=500_000)
    negative = make_fs(equity=-100_000, net_income=50_000, total_liabilities=900_000)

    result = _compute_avg_metrics([normal, negative])

    assert result.roe == round(normal.net_income / normal.equity * 100, 2)
    assert result.debt_ratio == round(normal.total_liabilities / normal.equity * 100, 2)
    assert result.operating_margin is not None
    assert result.roa is not None


def test_summary_export_csv_returns_text_csv(client, auth_headers, db_session):
    """format=csv 요청 시 Content-Type이 text/csv이고 헤더 행이 포함된다."""
    _add_fs(db_session, TEST_STOCK_CODE, "삼성전자", 2024)
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자&format=csv",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    content = response.content.decode("utf-8-sig")
    assert "사업연도" in content
    assert "매출액(원)" in content


def test_export_summary_csv_unit_header(client, auth_headers, db_session):
    """summary CSV export 헤더는 요청 단위를 금액 컬럼명에 표시한다."""
    _add_fs(db_session, TEST_STOCK_CODE, "삼성전자", 2024)
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        eok_response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자&format=csv&unit=억",
            headers=auth_headers,
        )
        won_response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자&format=csv",
            headers=auth_headers,
        )

    assert eok_response.status_code == 200
    assert won_response.status_code == 200
    eok_header = eok_response.content.decode("utf-8-sig").splitlines()[0]
    won_header = won_response.content.decode("utf-8-sig").splitlines()[0]
    assert "매출액(억원)" in eok_header
    assert "매출액(원)" in won_header


def test_export_summary_csv_unit_scales_values(client, auth_headers, db_session):
    """summary CSV export unit=억 요청은 금액 값을 억 단위로 변환한다."""
    _add_fs(
        db_session,
        TEST_STOCK_CODE,
        "삼성전자",
        2024,
        revenue=100_000_000_000,
    )
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자&format=csv&unit=억",
            headers=auth_headers,
        )

    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows[0][3] == "매출액(억원)"
    assert rows[1][3] == "1000"


def test_summary_export_pdf_still_works(client, auth_headers, db_session):
    """format 파라미터 없이 요청하면 기존 PDF를 반환한다."""
    _add_fs(db_session, TEST_STOCK_CODE, "삼성전자", 2024)
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get(
            "/api/v1/finance/summary/export?keyword=삼성전자",
            headers=auth_headers,
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"


def test_compare_export_csv_returns_text_csv(client, auth_headers, db_session):
    """비교 CSV export가 text/csv를 반환하고 기업명이 포함된다."""
    SECOND_CODE = "000660"
    _add_fs(db_session, TEST_STOCK_CODE, "삼성전자", 2024)
    _add_fs(db_session, SECOND_CODE, "SK하이닉스", 2024)
    db_session.commit()

    def resolve(keyword):
        return TEST_STOCK_CODE if "삼성" in keyword else SECOND_CODE

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve):
        response = client.get(
            "/api/v1/finance/compare/export?keywords=삼성전자&keywords=SK하이닉스&format=csv",
            headers=auth_headers,
        )

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    content = response.content.decode("utf-8-sig")
    assert "삼성전자" in content
    assert "SK하이닉스" in content


def test_export_compare_csv_unit_header(client, auth_headers, db_session):
    """비교 CSV export 헤더는 요청 단위를 금액 컬럼명에 표시한다."""
    SECOND_CODE = "000660"
    _add_fs(db_session, TEST_STOCK_CODE, "삼성전자", 2024)
    _add_fs(db_session, SECOND_CODE, "SK하이닉스", 2024)
    db_session.commit()

    def resolve(keyword):
        return TEST_STOCK_CODE if "삼성" in keyword else SECOND_CODE

    with patch("app.api.v1.finance.get_resolved_code", side_effect=resolve):
        eok_response = client.get(
            "/api/v1/finance/compare/export?keywords=삼성전자&keywords=SK하이닉스&format=csv&unit=억",
            headers=auth_headers,
        )
        won_response = client.get(
            "/api/v1/finance/compare/export?keywords=삼성전자&keywords=SK하이닉스&format=csv",
            headers=auth_headers,
        )

    assert eok_response.status_code == 200
    assert won_response.status_code == 200
    eok_header = eok_response.content.decode("utf-8-sig").splitlines()[0]
    won_header = won_response.content.decode("utf-8-sig").splitlines()[0]
    assert "매출액(억원)" in eok_header
    assert "매출액(원)" in won_header


# ── Feature 4: 섹터 정보 ─────────────────────────────────────────────────────

def test_summary_includes_sector_field(client, auth_headers, db_session):
    """summary 응답에 sector 필드가 포함된다 (DART 미초기화 시 null)."""
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE, company_name="삼성전자",
        fiscal_year=2024, revenue=1000, operating_profit=100, net_income=80,
        total_assets=2000, total_liabilities=500, equity=1500,
        cost_of_sales=0, gross_profit=0, sga=0, cash=100,
        created_at=datetime.now(timezone.utc),
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE):
        response = client.get("/api/v1/finance/summary?keyword=삼성전자", headers=auth_headers)

    assert response.status_code == 200
    assert "sector" in response.json()


def test_companies_includes_sector_field(client, auth_headers, db_session):
    """companies 목록 각 항목에 sector 필드가 포함된다."""
    _add_fs(db_session, TEST_STOCK_CODE, "삼성전자", 2024)
    db_session.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="completed"))
    db_session.commit()

    response = client.get("/api/v1/finance/companies", headers=auth_headers)
    assert response.status_code == 200
    items = response.json()
    assert len(items) > 0
    assert "sector" in items[0]


def test_summary_trend_with_data(client, auth_headers, test_engine):
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=test_engine)
    db = Session()
    for year in [2022, 2023]:
        db.add(FinancialStatement(
            company_id=TEST_STOCK_CODE,
            company_name="Samsung Electronics",
            fiscal_year=year,
            revenue=1000,
            operating_profit=100,
            net_income=80,
            total_assets=2000,
            total_liabilities=500,
            equity=1500,
        ))
    db.commit()
    db.close()

    with patch("app.api.v1.finance.DartService.resolve_stock_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.DartService._init_failed", False), \
         patch("app.api.v1.finance.DartService._stock_corps", {TEST_STOCK_CODE: object()}), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value=None):
        resp = client.get(
            f"/api/v1/finance/summary/trend?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "history" in data
    assert isinstance(data["history"], list)
    assert len(data["history"]) == 2
    years = [item["fiscal_year"] for item in data["history"]]
    assert years == sorted(years)


def test_summary_detail_returns_summary_and_trend(client, auth_headers, db_session):
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE,
        company_name="Samsung Electronics",
        fiscal_year=2023,
        revenue=300_000_000_000,
        operating_profit=30_000_000_000,
        total_assets=500_000_000_000,
        total_liabilities=200_000_000_000,
        equity=300_000_000_000,
    ))
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE,
        company_name="Samsung Electronics",
        fiscal_year=2022,
        revenue=250_000_000_000,
        operating_profit=20_000_000_000,
        total_assets=480_000_000_000,
        total_liabilities=210_000_000_000,
        equity=270_000_000_000,
    ))
    db_session.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="completed"))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value=None), \
         patch("app.api.v1.finance._write_search_log"):
        resp = client.get(
            f"/api/v1/finance/summary/detail?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "summary" in data
    assert "trend" in data
    assert data["collection_status"] == "completed"
    assert data["summary"]["stock_code"] == TEST_STOCK_CODE
    assert len(data["trend"]["history"]) == 2


def test_summary_detail_logs_search_on_success(client, auth_headers, db_session):
    db_session.add(FinancialStatement(
        company_id=TEST_STOCK_CODE,
        company_name="Samsung Electronics",
        fiscal_year=2024,
        revenue=1,
        operating_profit=1,
        net_income=1,
        total_assets=1,
        total_liabilities=0,
        equity=1,
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value=None), \
         patch("app.api.v1.finance._write_search_log") as mock_log:
        client.get(
            f"/api/v1/finance/summary/detail?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    mock_log.assert_called_once_with(TEST_STOCK_CODE, "Samsung Electronics", None)


def test_summary_sector_rank_no_peers(client, auth_headers, test_engine):
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=test_engine)
    db = Session()
    db.add(FinancialStatement(
        company_id=TEST_STOCK_CODE,
        company_name="Samsung Electronics",
        fiscal_year=2023,
        revenue=300_000_000_000,
    ))
    db.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="completed"))
    db.commit()
    db.close()

    with patch("app.api.v1.finance.DartService.resolve_stock_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.DartService._init_failed", False), \
         patch("app.api.v1.finance.DartService._stock_corps", {TEST_STOCK_CODE: object()}), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value="Semiconductor"):
        resp = client.get(
            f"/api/v1/finance/summary?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    assert resp.json()["sector_rank"] is None


def test_summary_sector_rank_with_peers(client, auth_headers, test_engine):
    from sqlalchemy.orm import sessionmaker

    peer_code = "000660"
    Session = sessionmaker(bind=test_engine)
    db = Session()
    db.add(FinancialStatement(
        company_id=TEST_STOCK_CODE,
        company_name="Samsung Electronics",
        fiscal_year=2023,
        revenue=300_000_000_000,
        operating_profit=30_000_000_000,
    ))
    db.add(FinancialStatement(
        company_id=peer_code,
        company_name="SK Hynix",
        fiscal_year=2023,
        revenue=100_000_000_000,
        operating_profit=5_000_000_000,
    ))
    db.add(CollectionTask(stock_code=TEST_STOCK_CODE, status="completed"))
    db.add(CollectionTask(stock_code=peer_code, status="completed"))
    db.commit()
    db.close()

    def mock_sector(code):
        return "Semiconductor" if code in (TEST_STOCK_CODE, peer_code) else None

    with patch("app.api.v1.finance.DartService.resolve_stock_code", return_value=TEST_STOCK_CODE), \
         patch("app.api.v1.finance.DartService._init_failed", False), \
         patch("app.api.v1.finance.DartService._stock_corps", {TEST_STOCK_CODE: object(), peer_code: object()}), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", side_effect=mock_sector):
        resp = client.get(
            f"/api/v1/finance/summary?keyword={TEST_STOCK_CODE}",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    rank = resp.json()["sector_rank"]
    assert rank is not None
    assert rank["total_companies"] == 2
    assert rank["revenue_rank"] == 1
    assert rank["operating_margin_rank"] == 1


def test_companies_min_revenue_filter(client, auth_headers, test_engine):
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=test_engine)
    db = Session()
    db.add(FinancialStatement(
        company_id="000001",
        company_name="Large Company",
        fiscal_year=2023,
        revenue=2_000_000_000_000,
    ))
    db.add(FinancialStatement(
        company_id="000002",
        company_name="Small Company",
        fiscal_year=2023,
        revenue=50_000_000_000,
    ))
    db.add(CollectionTask(stock_code="000001", status="completed"))
    db.add(CollectionTask(stock_code="000002", status="completed"))
    db.commit()
    db.close()

    with patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value=None):
        resp = client.get(
            "/api/v1/finance/companies?min_revenue=1000000000000",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    codes = [c["stock_code"] for c in resp.json()]
    assert "000001" in codes
    assert "000002" not in codes


def test_companies_min_operating_margin_filter(client, auth_headers, test_engine):
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=test_engine)
    db = Session()
    db.add(FinancialStatement(
        company_id="000011",
        company_name="High Margin Company",
        fiscal_year=2023,
        revenue=1_000_000_000_000,
        operating_profit=200_000_000_000,
    ))
    db.add(FinancialStatement(
        company_id="000012",
        company_name="Low Margin Company",
        fiscal_year=2023,
        revenue=1_000_000_000_000,
        operating_profit=30_000_000_000,
    ))
    db.add(CollectionTask(stock_code="000011", status="completed"))
    db.add(CollectionTask(stock_code="000012", status="completed"))
    db.commit()
    db.close()

    with patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value=None):
        resp = client.get(
            "/api/v1/finance/companies?min_operating_margin=10",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    codes = [c["stock_code"] for c in resp.json()]
    assert "000011" in codes
    assert "000012" not in codes


def test_companies_max_debt_ratio_filter(client, auth_headers, test_engine):
    from sqlalchemy.orm import sessionmaker

    Session = sessionmaker(bind=test_engine)
    db = Session()
    db.add(FinancialStatement(
        company_id="000021",
        company_name="Stable Company",
        fiscal_year=2023,
        total_liabilities=500_000_000_000,
        equity=1_000_000_000_000,
    ))
    db.add(FinancialStatement(
        company_id="000022",
        company_name="Leveraged Company",
        fiscal_year=2023,
        total_liabilities=3_000_000_000_000,
        equity=1_000_000_000_000,
    ))
    db.add(CollectionTask(stock_code="000021", status="completed"))
    db.add(CollectionTask(stock_code="000022", status="completed"))
    db.commit()
    db.close()

    with patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value=None):
        resp = client.get(
            "/api/v1/finance/companies?max_debt_ratio=100",
            headers=auth_headers,
        )

    assert resp.status_code == 200
    codes = [c["stock_code"] for c in resp.json()]
    assert "000021" in codes
    assert "000022" not in codes


def test_compare_save_and_fetch(client, auth_headers, db_session):
    for code, name in [("005930", "Samsung Electronics"), ("000660", "SK hynix")]:
        db_session.add(FinancialStatement(
            company_id=code,
            company_name=name,
            fiscal_year=2023,
            revenue=200_000_000_000,
            operating_profit=20_000_000_000,
            total_assets=300_000_000_000,
            total_liabilities=100_000_000_000,
            equity=200_000_000_000,
        ))
        db_session.add(CollectionTask(stock_code=code, status="completed"))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", side_effect=lambda k: k), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value=None), \
         patch("app.api.v1.finance._write_search_log"):
        save_resp = client.post(
            "/api/v1/finance/compare/save?keywords=005930&keywords=000660",
            headers=auth_headers,
        )
        assert save_resp.status_code == 201
        share_id = save_resp.json()["share_id"]
        assert len(share_id) == 12

        get_resp = client.get(f"/api/v1/finance/compare/{share_id}")

    assert get_resp.status_code == 200
    data = get_resp.json()
    assert len(data["companies"]) == 2


def test_compare_share_expired_returns_404(client, db_session):
    from datetime import timedelta
    from app.db.models import CompareShare

    db_session.add(CompareShare(
        share_id="deadbeef",
        payload='{"companies": [], "compared_at": "2026-01-01T00:00:00+00:00"}',
        expires_at=datetime.now(timezone.utc) - timedelta(days=1),
    ))
    db_session.commit()

    resp = client.get("/api/v1/finance/compare/deadbeef")

    assert resp.status_code == 404


def test_compare_share_not_found(client):
    resp = client.get("/api/v1/finance/compare/abcdef123456")

    assert resp.status_code == 404


def test_similar_returns_same_sector_similar_size(client, auth_headers, db_session):
    db_session.add(FinancialStatement(
        company_id="REF001",
        company_name="Reference",
        fiscal_year=2023,
        revenue=10_000_000_000,
        operating_profit=2_000_000_000,
        total_assets=8_000_000_000,
        total_liabilities=2_000_000_000,
        equity=6_000_000_000,
    ))
    db_session.add(FinancialStatement(
        company_id="REF001",
        company_name="Reference",
        fiscal_year=2022,
        revenue=8_000_000_000,
        operating_profit=1_000_000_000,
        total_assets=7_000_000_000,
        total_liabilities=2_000_000_000,
        equity=5_000_000_000,
    ))
    db_session.add(FinancialStatement(
        company_id="CAND01",
        company_name="Candidate A",
        fiscal_year=2023,
        revenue=8_000_000_000,
        operating_profit=1_600_000_000,
        total_assets=6_000_000_000,
        total_liabilities=2_000_000_000,
        equity=4_000_000_000,
    ))
    db_session.add(FinancialStatement(
        company_id="CAND01",
        company_name="Candidate A",
        fiscal_year=2022,
        revenue=6_000_000_000,
        operating_profit=800_000_000,
        total_assets=5_000_000_000,
        total_liabilities=2_000_000_000,
        equity=3_000_000_000,
    ))
    db_session.add(FinancialStatement(
        company_id="CAND02",
        company_name="Candidate B Too Large",
        fiscal_year=2023,
        revenue=20_000_000_000,
        operating_profit=4_000_000_000,
        total_assets=15_000_000_000,
        total_liabilities=5_000_000_000,
        equity=10_000_000_000,
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", side_effect=lambda keyword: keyword), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value="Electronics"):
        resp = client.get("/api/v1/finance/similar?keyword=REF001&limit=5", headers=auth_headers)

    assert resp.status_code == 200
    codes = [item["stock_code"] for item in resp.json()]
    assert codes == ["CAND01"]


def test_similar_no_reference_data(client, auth_headers):
    with patch("app.api.v1.finance.get_resolved_code", return_value="999999"):
        resp = client.get("/api/v1/finance/similar?keyword=999999", headers=auth_headers)

    assert resp.status_code == 404


def test_similar_no_matches_returns_empty(client, auth_headers, db_session):
    db_session.add(FinancialStatement(
        company_id="LONELY",
        company_name="Lonely",
        fiscal_year=2023,
        revenue=10_000_000_000,
        operating_profit=2_000_000_000,
        total_assets=8_000_000_000,
        total_liabilities=2_000_000_000,
        equity=6_000_000_000,
    ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value="LONELY"), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value="SectorX"):
        resp = client.get("/api/v1/finance/similar?keyword=LONELY", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json() == []


def test_recommendation_filters_and_sector_cap(client, auth_headers, db_session):
    fresh = datetime.now(timezone.utc) - timedelta(days=10)
    stale = datetime.now(timezone.utc) - timedelta(days=200)

    for code, revenue, operating_profit in [
        ("IT01", 10_000_000_000, 3_000_000_000),
        ("IT02", 9_000_000_000, 2_000_000_000),
        ("IT03", 8_000_000_000, 1_500_000_000),
    ]:
        db_session.add(FinancialStatement(
            company_id=code,
            company_name=f"IT {code}",
            fiscal_year=2023,
            revenue=revenue,
            operating_profit=operating_profit,
            total_assets=int(revenue * 1.2),
            total_liabilities=int(revenue * 0.3),
            equity=int(revenue * 0.9),
            created_at=fresh,
        ))
        db_session.add(FinancialStatement(
            company_id=code,
            company_name=f"IT {code}",
            fiscal_year=2022,
            revenue=int(revenue * 0.8),
            operating_profit=int(operating_profit * 0.7),
            total_assets=revenue,
            total_liabilities=int(revenue * 0.3),
            equity=int(revenue * 0.7),
            created_at=fresh,
        ))
    db_session.add(FinancialStatement(
        company_id="BIO01",
        company_name="Bio One",
        fiscal_year=2023,
        revenue=5_000_000_000,
        operating_profit=1_500_000_000,
        total_assets=6_000_000_000,
        total_liabilities=1_000_000_000,
        equity=5_000_000_000,
        created_at=fresh,
    ))
    db_session.add(FinancialStatement(
        company_id="BIO01",
        company_name="Bio One",
        fiscal_year=2022,
        revenue=4_000_000_000,
        operating_profit=800_000_000,
        total_assets=5_000_000_000,
        total_liabilities=1_000_000_000,
        equity=4_000_000_000,
        created_at=fresh,
    ))
    db_session.add(FinancialStatement(
        company_id="OLD01",
        company_name="Old Company",
        fiscal_year=2023,
        revenue=10_000_000_000,
        operating_profit=3_000_000_000,
        total_assets=12_000_000_000,
        total_liabilities=3_000_000_000,
        equity=9_000_000_000,
        created_at=stale,
    ))
    db_session.commit()

    def fake_sector(code):
        if code.startswith("IT"):
            return "IT"
        if code.startswith("BIO"):
            return "BIO"
        return None

    with patch("app.api.v1.finance.DartService.get_sector_by_stock_code", side_effect=fake_sector):
        resp = client.get("/api/v1/finance/recommendation?limit=10", headers=auth_headers)

    assert resp.status_code == 200
    codes = [item["stock_code"] for item in resp.json()]
    assert sum(1 for code in codes if code.startswith("IT")) == 2
    assert "BIO01" in codes
    assert "OLD01" not in codes


def test_recommendation_excludes_capital_impairment(client, auth_headers, db_session):
    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    for code, equity in [("BADCAP", -100_000_000), ("GOODCAP", 7_000_000_000)]:
        db_session.add(FinancialStatement(
            company_id=code,
            company_name=code,
            fiscal_year=2023,
            revenue=10_000_000_000,
            operating_profit=3_000_000_000,
            total_assets=12_000_000_000,
            total_liabilities=3_000_000_000,
            equity=equity,
            created_at=fresh,
        ))
        db_session.add(FinancialStatement(
            company_id=code,
            company_name=code,
            fiscal_year=2022,
            revenue=8_000_000_000,
            operating_profit=1_000_000_000,
            total_assets=10_000_000_000,
            total_liabilities=2_000_000_000,
            equity=7_000_000_000,
            created_at=fresh,
        ))
    db_session.commit()

    with patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value="IT"):
        resp = client.get("/api/v1/finance/recommendation?limit=10", headers=auth_headers)

    assert resp.status_code == 200
    codes = [item["stock_code"] for item in resp.json()]
    assert "BADCAP" not in codes
    assert "GOODCAP" in codes


def test_recommendation_excludes_three_year_operating_loss(client, auth_headers, db_session):
    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    for year, revenue, operating_profit in [
        (2023, 12_000_000_000, -100_000_000),
        (2022, 10_000_000_000, -500_000_000),
        (2021, 8_000_000_000, -900_000_000),
    ]:
        db_session.add(FinancialStatement(
            company_id="LOSS3",
            company_name="Loss Three",
            fiscal_year=year,
            revenue=revenue,
            operating_profit=operating_profit,
            total_assets=12_000_000_000,
            total_liabilities=2_000_000_000,
            equity=10_000_000_000,
            created_at=fresh,
        ))
    for year, revenue, operating_profit in [
        (2023, 12_000_000_000, 3_000_000_000),
        (2022, 10_000_000_000, 1_000_000_000),
    ]:
        db_session.add(FinancialStatement(
            company_id="GOOD3",
            company_name="Good Three",
            fiscal_year=year,
            revenue=revenue,
            operating_profit=operating_profit,
            total_assets=12_000_000_000,
            total_liabilities=2_000_000_000,
            equity=10_000_000_000,
            created_at=fresh,
        ))
    db_session.commit()

    with patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value="IT"):
        resp = client.get("/api/v1/finance/recommendation?limit=10", headers=auth_headers)

    assert resp.status_code == 200
    codes = [item["stock_code"] for item in resp.json()]
    assert "LOSS3" not in codes
    assert "GOOD3" in codes


def test_recommendation_composite_score_orders_growth_over_margin(client, auth_headers, db_session):
    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    for code, latest_revenue, latest_profit, previous_revenue, previous_profit in [
        ("GROW01", 20_000_000_000, 800_000_000, 10_000_000_000, 100_000_000),
        ("MARG01", 10_100_000_000, 3_000_000_000, 10_000_000_000, 1_000_000_000),
    ]:
        db_session.add(FinancialStatement(
            company_id=code,
            company_name=code,
            fiscal_year=2023,
            revenue=latest_revenue,
            operating_profit=latest_profit,
            total_assets=latest_revenue,
            total_liabilities=1_000_000_000,
            equity=9_000_000_000,
            created_at=fresh,
        ))
        db_session.add(FinancialStatement(
            company_id=code,
            company_name=code,
            fiscal_year=2022,
            revenue=previous_revenue,
            operating_profit=previous_profit,
            total_assets=previous_revenue,
            total_liabilities=1_000_000_000,
            equity=9_000_000_000,
            created_at=fresh,
        ))
    db_session.commit()

    with patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value="IT"):
        resp = client.get("/api/v1/finance/recommendation?limit=10", headers=auth_headers)

    assert resp.status_code == 200
    codes = [item["stock_code"] for item in resp.json()]
    assert codes[:2] == ["GROW01", "MARG01"]
    assert "score" not in resp.json()[0]


def test_recommendation_empty_when_no_data(client, auth_headers):
    resp = client.get("/api/v1/finance/recommendation", headers=auth_headers)

    assert resp.status_code == 200
    assert resp.json() == []


def test_compare_share_invalid_format_returns_422(client, auth_headers):
    resp = client.get("/api/v1/finance/compare/INVALID!", headers=auth_headers)

    assert resp.status_code == 422


def test_compare_share_short_format_returns_422(client, auth_headers):
    resp = client.get("/api/v1/finance/compare/abcdef0", headers=auth_headers)

    assert resp.status_code == 422


def test_sector_cache_avoids_duplicate_dart_call():
    from app.api.v1 import finance as finance_module

    with patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value="IT") as mock_sector:
        assert finance_module._safe_get_sector("005930") == "IT"
        assert finance_module._safe_get_sector("005930") == "IT"
        assert mock_sector.call_count == 1

        assert finance_module._safe_get_sector("000660") == "IT"
        assert mock_sector.call_count == 2


def test_recommendation_cache_hit_returns_same_result(client, auth_headers, db_session):
    from app.api.v1 import finance as finance_module

    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.add(FinancialStatement(
        company_id="GOOD01",
        company_name="Good One",
        fiscal_year=2023,
        revenue=10_000_000_000,
        operating_profit=3_000_000_000,
        total_assets=12_000_000_000,
        total_liabilities=3_000_000_000,
        equity=9_000_000_000,
        created_at=fresh,
    ))
    db_session.add(FinancialStatement(
        company_id="GOOD01",
        company_name="Good One",
        fiscal_year=2022,
        revenue=8_000_000_000,
        operating_profit=1_000_000_000,
        total_assets=10_000_000_000,
        total_liabilities=3_000_000_000,
        equity=7_000_000_000,
        created_at=fresh,
    ))
    db_session.commit()

    with patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value="IT"):
        first = client.get("/api/v1/finance/recommendation?limit=5", headers=auth_headers)
        assert first.status_code == 200
        first_data = first.json()
        assert [item["stock_code"] for item in first_data] == ["GOOD01"]

        latest = (
            db_session.query(FinancialStatement)
            .filter(
                FinancialStatement.company_id == "GOOD01",
                FinancialStatement.fiscal_year == 2023,
            )
            .first()
        )
        latest.operating_profit = 500_000_000
        latest.total_liabilities = 20_000_000_000
        db_session.commit()

        second = client.get("/api/v1/finance/recommendation?limit=5", headers=auth_headers)
        assert second.status_code == 200
        assert second.json() == first_data

        finance_module._recommendation_cache.clear()
        third = client.get("/api/v1/finance/recommendation?limit=5", headers=auth_headers)
        assert third.status_code == 200
        assert third.json() == []


def test_similar_cache_keyed_by_stock_code_and_limit(client, auth_headers, db_session):
    from app.api.v1 import finance as finance_module

    for code, revenue in [("REF001", 10_000_000_000), ("CAND01", 8_000_000_000)]:
        db_session.add(FinancialStatement(
            company_id=code,
            company_name=code,
            fiscal_year=2023,
            revenue=revenue,
            operating_profit=2_000_000_000,
            total_assets=8_000_000_000,
            total_liabilities=2_000_000_000,
            equity=6_000_000_000,
        ))
        db_session.add(FinancialStatement(
            company_id=code,
            company_name=code,
            fiscal_year=2022,
            revenue=int(revenue * 0.8),
            operating_profit=1_000_000_000,
            total_assets=7_000_000_000,
            total_liabilities=2_000_000_000,
            equity=5_000_000_000,
        ))
    db_session.commit()

    with patch("app.api.v1.finance.get_resolved_code", return_value="REF001"), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value="IT"):
        first = client.get("/api/v1/finance/similar?keyword=REF001&limit=5", headers=auth_headers)
        assert first.status_code == 200
        first_data = first.json()
        assert [item["stock_code"] for item in first_data] == ["CAND01"]

        candidate_latest = (
            db_session.query(FinancialStatement)
            .filter(
                FinancialStatement.company_id == "CAND01",
                FinancialStatement.fiscal_year == 2023,
            )
            .first()
        )
        candidate_latest.revenue = 30_000_000_000
        db_session.commit()

        second = client.get("/api/v1/finance/similar?keyword=REF001&limit=5", headers=auth_headers)
        assert second.status_code == 200
        assert second.json() == first_data

        third = client.get("/api/v1/finance/similar?keyword=REF001&limit=3", headers=auth_headers)
        assert third.status_code == 200
        assert third.json() == []
        assert ("REF001", 5) in finance_module._similar_cache
        assert ("REF001", 3) in finance_module._similar_cache


def test_warm_recommendation_cache_populates_cache(client, auth_headers, db_session):
    from app.api.v1 import finance as finance_module

    fresh = datetime.now(timezone.utc) - timedelta(days=1)
    db_session.add(FinancialStatement(
        company_id="WARM01",
        company_name="Warm One",
        fiscal_year=2023,
        revenue=10_000_000_000,
        operating_profit=3_000_000_000,
        total_assets=12_000_000_000,
        total_liabilities=3_000_000_000,
        equity=9_000_000_000,
        created_at=fresh,
    ))
    db_session.add(FinancialStatement(
        company_id="WARM01",
        company_name="Warm One",
        fiscal_year=2022,
        revenue=8_000_000_000,
        operating_profit=1_000_000_000,
        total_assets=10_000_000_000,
        total_liabilities=3_000_000_000,
        equity=7_000_000_000,
        created_at=fresh,
    ))
    db_session.commit()

    with patch("app.api.v1.finance.DartService.get_sector_by_stock_code", return_value="IT"):
        finance_module.warm_recommendation_cache(db_session, limits=[5])

        cached = finance_module._recommendation_cache[5]
        assert cached["expires_at"] > datetime.now(timezone.utc)
        assert [item.stock_code for item in cached["value"]] == ["WARM01"]

        latest = (
            db_session.query(FinancialStatement)
            .filter(
                FinancialStatement.company_id == "WARM01",
                FinancialStatement.fiscal_year == 2023,
            )
            .first()
        )
        latest.operating_profit = 500_000_000
        latest.total_liabilities = 20_000_000_000
        db_session.commit()

        resp = client.get("/api/v1/finance/recommendation?limit=5", headers=auth_headers)

    assert resp.status_code == 200
    assert [item["stock_code"] for item in resp.json()] == ["WARM01"]
