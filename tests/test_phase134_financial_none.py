"""Phase 134: 금융업 미수집 재무값은 None으로 저장되어야 한다."""

from unittest.mock import MagicMock, patch

from sqlalchemy.orm import sessionmaker

from app.db.models import CollectionTask, FinancialStatement
from app.services.dart_service import DartService


NONE_FIELDS = ("revenue", "cost_of_sales", "gross_profit", "sga", "operating_profit")


def test_financial_statement_preserves_explicit_none(test_engine):
    Session = sessionmaker(bind=test_engine)
    with Session() as db:
        db.add(FinancialStatement(
            company_id="105560",
            company_name="KB금융",
            fiscal_year=2024,
            **{field: None for field in NONE_FIELDS},
        ))
        db.commit()

    with Session() as db:
        row = db.query(FinancialStatement).filter_by(company_id="105560").one()
        assert {field: getattr(row, field) for field in NONE_FIELDS} == {
            field: None for field in NONE_FIELDS
        }


def test_financial_statement_preserves_values_including_zero(test_engine):
    Session = sessionmaker(bind=test_engine)
    with Session() as db:
        db.add(FinancialStatement(
            company_id="005930",
            company_name="삼성전자",
            fiscal_year=2024,
            **dict.fromkeys(NONE_FIELDS, 100),
        ))
        db.add(FinancialStatement(
            company_id="000001",
            company_name="테스트기업",
            fiscal_year=2024,
            **dict.fromkeys(NONE_FIELDS, 0),
        ))
        db.commit()

    with Session() as db:
        valued = db.query(FinancialStatement).filter_by(company_id="005930").one()
        zeroed = db.query(FinancialStatement).filter_by(company_id="000001").one()
        assert [getattr(valued, field) for field in NONE_FIELDS] == [100] * 5
        assert [getattr(zeroed, field) for field in NONE_FIELDS] == [0] * 5


def test_financial_corrections_are_persisted_as_none(test_engine):
    Session = sessionmaker(bind=test_engine)
    with Session() as db:
        db.add(CollectionTask(stock_code="105560", status="processing"))
        db.commit()

    corp_list = MagicMock()
    corp = MagicMock(corp_name="KB금융", sector="은행")
    corp_list.find_by_stock_code.return_value = corp
    extracted = {2024: {
        "revenue": 999,
        "operating_profit": 999,
        "net_income": 100,
        "total_assets": 1_000,
        "total_liabilities": 900,
        "equity": 100,
        "cash": 50,
    }}

    with patch.object(DartService, "_global_corp_list", corp_list), \
         patch.object(DartService, "_collect_financial_data", return_value=extracted), \
         patch.object(DartService, "_is_financial_sector", return_value=True), \
         patch.object(DartService, "classify_financial_sector", return_value="bank"), \
         patch("app.db.database.SessionLocal", Session):
        DartService.fetch_and_process_data("105560")

    with Session() as db:
        row = db.query(FinancialStatement).filter_by(company_id="105560").one()
        assert row.revenue is None
        assert row.operating_profit is None
        assert row.cost_of_sales is None
        assert row.gross_profit is None
        assert row.sga is None


def test_financial_peer_revenue_rank_is_none(client, auth_headers, test_engine):
    Session = sessionmaker(bind=test_engine)
    rows = [
        ("105560", "KB금융"),
        ("055550", "신한지주"),
        ("086790", "하나금융지주"),
    ]
    with Session() as db:
        for code, name in rows:
            db.add(FinancialStatement(
                company_id=code,
                company_name=name,
                fiscal_year=2024,
                revenue=None,
                operating_profit=None,
                net_income=100,
                total_assets=1_000,
                total_liabilities=900,
                equity=100,
                source_report=f"{name} 2024년 사업보고서 [금융업]",
            ))
            db.add(CollectionTask(stock_code=code, status="completed"))
        db.commit()

    def sector(code):
        return "은행" if code in dict(rows) else None

    with patch("app.api.v1.finance.DartService.resolve_stock_code", return_value="105560"), \
         patch("app.api.v1.finance.DartService._init_failed", False), \
         patch("app.api.v1.finance.DartService._stock_corps", {code: object() for code, _ in rows}), \
         patch("app.api.v1.finance.DartService.get_sector_by_stock_code", side_effect=sector):
        response = client.get(
            "/api/v1/finance/summary?keyword=105560",
            headers=auth_headers,
        )

    assert response.status_code == 200
    rank = response.json()["sector_rank"]
    assert rank is not None
    assert rank["total_companies"] == 3
    assert rank["revenue_rank"] is None
