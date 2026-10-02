"""
로딩 시간 단축 검증 테스트

측정 항목:
  1. DB에 데이터가 있을 때 /summary 응답 시간 (즉시 반환 여부)
  2. 7일 이상 된 데이터에서 백그라운드 갱신이 트리거되는지
  3. 사전 수집 대상 선정 로직 (SearchLog 상위 종목)
"""
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from app.db.models import FinancialStatement, CollectionTask, SearchLog


# ── 공통 픽스처 ───────────────────────────────────────────────────────────────

def _insert_financial_data(db, stock_code: str, company_name: str, years: int = 3):
    """테스트용 재무 데이터 삽입."""
    for i in range(years):
        year = 2024 - i
        db.add(FinancialStatement(
            company_id=stock_code,
            company_name=company_name,
            fiscal_year=year,
            revenue=300_000_000_000_000 - i * 10_000_000_000_000,
            operating_profit=20_000_000_000_000,
            net_income=15_000_000_000_000,
            total_assets=500_000_000_000_000,
            total_liabilities=100_000_000_000_000,
            equity=400_000_000_000_000,
        ))
    db.commit()


def _insert_task(db, stock_code: str, status: str, days_ago: int):
    """CollectionTask를 지정 일수 전 updated_at으로 삽입."""
    updated = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.add(CollectionTask(
        stock_code=stock_code,
        status=status,
        started_at=updated,
        updated_at=updated,
    ))
    db.commit()


# ── 테스트 1: 기존 데이터 즉시 반환 ──────────────────────────────────────────

def test_summary_returns_immediately_when_data_exists(client, db_session, auth_headers):
    """DB에 데이터가 있을 때 /summary가 1초 이내에 응답하는지 검증."""
    _insert_financial_data(db_session, "005930", "삼성전자")
    _insert_task(db_session, "005930", "completed", days_ago=1)

    with patch("app.services.dart_service.DartService.resolve_stock_code", return_value="005930"), \
         patch("app.services.dart_service.DartService.get_sector_by_stock_code", return_value="전자"):

        start = time.perf_counter()
        resp = client.get("/api/v1/finance/summary?keyword=삼성전자", headers=auth_headers)
        elapsed = time.perf_counter() - start

    assert resp.status_code == 200, f"예상 200, 실제: {resp.status_code}"
    assert elapsed < 1.0, f"응답 시간 초과: {elapsed:.3f}s (기준 1.0s)"
    print(f"\n[즉시 반환] 응답 시간: {elapsed * 1000:.1f}ms")


# ── 테스트 2: TTL 만료 시 즉시 반환 + 백그라운드 갱신 트리거 ─────────────────

def test_stale_data_returns_immediately_and_triggers_refresh(client, db_session, auth_headers):
    """7일 이상 된 데이터도 즉시 반환하고, 백그라운드 갱신이 예약되는지 검증."""
    _insert_financial_data(db_session, "005380", "현대차")
    _insert_task(db_session, "005380", "completed", days_ago=10)  # 10일 전 데이터

    triggered = []

    def fake_silent_refresh(stock_code):
        triggered.append(stock_code)

    with patch("app.services.dart_service.DartService.resolve_stock_code", return_value="005380"), \
         patch("app.services.dart_service.DartService.get_sector_by_stock_code", return_value="자동차"), \
         patch("app.api.v1.finance._silent_refresh", side_effect=fake_silent_refresh):

        start = time.perf_counter()
        resp = client.get("/api/v1/finance/summary?keyword=현대차", headers=auth_headers)
        elapsed = time.perf_counter() - start

    assert resp.status_code == 200, f"예상 200, 실제: {resp.status_code}"
    assert elapsed < 1.0, f"응답 시간 초과: {elapsed:.3f}s — 재수집 대기가 발생했을 수 있음"
    assert "005380" in triggered, "백그라운드 갱신이 트리거되지 않음"
    print(f"\n[TTL 만료] 응답 시간: {elapsed * 1000:.1f}ms | 백그라운드 갱신 트리거: {triggered}")


# ── 테스트 3: 신선한 데이터는 백그라운드 갱신 안 함 ──────────────────────────

def test_fresh_data_does_not_trigger_refresh(client, db_session, auth_headers):
    """7일 이내 데이터는 백그라운드 갱신을 트리거하지 않는지 검증."""
    _insert_financial_data(db_session, "000660", "SK하이닉스")
    _insert_task(db_session, "000660", "completed", days_ago=3)  # 3일 전 — 신선함

    triggered = []

    with patch("app.services.dart_service.DartService.resolve_stock_code", return_value="000660"), \
         patch("app.services.dart_service.DartService.get_sector_by_stock_code", return_value="반도체"), \
         patch("app.api.v1.finance._silent_refresh", side_effect=lambda c: triggered.append(c)):

        resp = client.get("/api/v1/finance/summary?keyword=SK하이닉스", headers=auth_headers)

    assert resp.status_code == 200
    assert triggered == [], f"신선한 데이터인데 갱신 트리거됨: {triggered}"
    print(f"\n[신선 데이터] 백그라운드 갱신 없음 — 정상")


# ── 테스트 4: 데이터 없으면 404 (변경 없음 확인) ─────────────────────────────

def test_no_data_returns_404(client, auth_headers):
    """수집 데이터가 없으면 여전히 404를 반환하는지 검증."""
    with patch("app.services.dart_service.DartService.resolve_stock_code", return_value="999999"):
        resp = client.get("/api/v1/finance/summary?keyword=없는기업", headers=auth_headers)

    assert resp.status_code == 404
    print(f"\n[데이터 없음] 404 정상 반환")


# ── 테스트 5: 사전 수집 대상 선정 로직 ───────────────────────────────────────

def test_prewarm_targets_stale_popular_stocks(test_engine):
    """_prewarm_popular_stocks가 SearchLog 상위 종목 중 오래된 것만 수집하는지 검증."""
    from app.main import _prewarm_popular_stocks

    Session = sessionmaker(bind=test_engine)
    db = Session()

    # SearchLog: 삼성전자 5회, 현대차 3회, SK하이닉스 2회
    now = datetime.now(timezone.utc)
    for _ in range(5):
        db.add(SearchLog(stock_code="005930", company_name="삼성전자", searched_at=now))
    for _ in range(3):
        db.add(SearchLog(stock_code="005380", company_name="현대차", searched_at=now))
    for _ in range(2):
        db.add(SearchLog(stock_code="000660", company_name="SK하이닉스", searched_at=now))

    # 삼성전자: 10일 전 데이터 (stale → 수집 대상)
    db.add(CollectionTask(
        stock_code="005930", status="completed",
        started_at=now - timedelta(days=10),
        updated_at=now - timedelta(days=10),
    ))
    # 현대차: 3일 전 데이터 (fresh → 스킵)
    db.add(CollectionTask(
        stock_code="005380", status="completed",
        started_at=now - timedelta(days=3),
        updated_at=now - timedelta(days=3),
    ))
    # SK하이닉스: CollectionTask 없음 (수집 대상)
    db.commit()
    db.close()

    collected = []

    def fake_fetch(stock_code):
        collected.append(stock_code)

    mock_corp_list = MagicMock()

    with patch("app.main.SessionLocal", return_value=Session()), \
         patch("app.services.dart_service.DartService._global_corp_list", mock_corp_list), \
         patch("app.services.dart_service.DartService._init_failed", False), \
         patch("app.services.dart_service.DartService.fetch_and_process_data", side_effect=fake_fetch):
        _prewarm_popular_stocks()

    assert "005930" in collected, "삼성전자(stale)는 수집되어야 함"
    assert "005380" not in collected, "현대차(fresh)는 수집되면 안 됨"
    print(f"\n[사전 수집] 대상 종목: {collected}")
    print(f"  삼성전자(10일): 수집됨 ✓")
    print(f"  현대차(3일): 스킵됨 ✓")
    print(f"  SK하이닉스(미수집): {'수집됨 ✓' if '000660' in collected else '스킵됨'}")


def test_induty_prewarm_only_completed_and_continues_after_failure(test_engine, caplog):
    from app.main import _prewarm_completed_induty_codes
    from app.services.dart_service import DartService
    Session = sessionmaker(bind=test_engine)
    with Session() as db:
        db.add_all([
            CollectionTask(stock_code="000001", status="completed"),
            CollectionTask(stock_code="000002", status="failed"),
            CollectionTask(stock_code="000003", status="processing"),
            CollectionTask(stock_code="000004", status="completed"),
        ])
        db.commit()
    with patch("app.main.SessionLocal", Session), \
         patch.object(DartService, "_global_corp_list", MagicMock()), \
         patch.object(DartService, "_init_failed", False), \
         patch.object(DartService, "_find_corp", side_effect=lambda code: MagicMock(corp_code=code)) as find, \
         patch.object(DartService, "_fetch_induty_code", side_effect=[OSError("offline"), "64992"]) as fetch:
        _prewarm_completed_induty_codes()
    assert {c.args[0] for c in find.call_args_list} == {"000001", "000004"}
    assert fetch.call_count == 2
    assert "offline" in caplog.text


def test_induty_prewarm_database_failure_is_only_warning(caplog):
    from app.main import _prewarm_completed_induty_codes
    from app.services.dart_service import DartService
    with patch("app.main.SessionLocal", side_effect=OSError("db offline")), \
         patch.object(DartService, "_global_corp_list", MagicMock()), \
         patch.object(DartService, "_init_failed", False):
        _prewarm_completed_induty_codes()
    assert "db offline" in caplog.text
