"""
로딩 시간 개선 벤치마크

[개선 전] 재조회 시나리오
  - 데이터가 7일 이상 됐을 때: /collect/refresh → 폴링(~15-30초) → /summary
  - 사용자 대기 시간 = DART 수집 시간 전체

[개선 후]
  - /summary 즉시 반환 (DB 캐시)
  - DART 재수집은 백그라운드에서 조용히 진행
"""
import time
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from app.db.models import FinancialStatement, CollectionTask, SearchLog


DART_COLLECTION_SECONDS = 20  # 실제 DART 수집 평균 소요 시간 (초)


def _insert_stale_data(db, stock_code, company_name):
    """10일 전에 수집된 데이터 삽입 (TTL 만료 상태)."""
    for i in range(3):
        db.add(FinancialStatement(
            company_id=stock_code, company_name=company_name,
            fiscal_year=2024 - i,
            revenue=300_000_000_000_000,
            operating_profit=20_000_000_000_000,
            net_income=15_000_000_000_000,
            total_assets=500_000_000_000_000,
            total_liabilities=100_000_000_000_000,
            equity=400_000_000_000_000,
        ))
    stale = datetime.now(timezone.utc) - timedelta(days=10)
    db.add(CollectionTask(stock_code=stock_code, status="completed",
                          started_at=stale, updated_at=stale))
    db.commit()


# ── 시나리오 1: 개선 전 — 사용자가 직접 /collect/refresh + 폴링해야 하는 흐름 ─

@pytest.mark.slow
def test_scenario_before_improvement(client, db_session, auth_headers):
    """
    개선 전: 데이터가 오래됐을 때 사용자가 겪던 흐름 시뮬레이션
    /collect/refresh → /collect/status 폴링 → /summary
    """
    _insert_stale_data(db_session, "005930", "삼성전자")

    # DART 수집을 20초짜리 작업으로 시뮬레이션
    collect_done = threading.Event()

    def slow_collect(stock_code):
        time.sleep(DART_COLLECTION_SECONDS)
        collect_done.set()

    with patch("app.services.dart_service.DartService.resolve_stock_code", return_value="005930"), \
         patch("app.services.dart_service.DartService.get_sector_by_stock_code", return_value="전자"), \
         patch("app.api.v1.finance._collect_with_email", side_effect=lambda c, e: slow_collect(c)):

        # Step 1: 갱신 시작
        t0 = time.perf_counter()
        r = client.post("/api/v1/finance/collect/refresh?keyword=삼성전자", headers=auth_headers)
        assert r.status_code == 200

        # Step 2: 수집 완료까지 폴링 (시뮬레이션)
        collect_done.wait(timeout=DART_COLLECTION_SECONDS + 5)
        elapsed_collect = time.perf_counter() - t0

        # Step 3: /summary 호출
        t1 = time.perf_counter()
        r2 = client.get("/api/v1/finance/summary?keyword=삼성전자", headers=auth_headers)
        elapsed_summary = time.perf_counter() - t1

    total = elapsed_collect + elapsed_summary
    print(f"""
┌─────────────────────────────────────────────┐
│  [개선 전] 재조회 시나리오 (stale data)        │
├─────────────────────────────────────────────┤
│  /collect/refresh + 폴링 대기  : {elapsed_collect:6.1f}s  │
│  /summary 응답               : {elapsed_summary * 1000:6.1f}ms │
│  사용자 총 대기 시간           : {total:6.1f}s  │
└─────────────────────────────────────────────┘""")

    assert r2.status_code == 200


# ── 시나리오 2: 개선 후 — /summary 즉시 반환 ──────────────────────────────────

def test_scenario_after_improvement(client, db_session, auth_headers):
    """
    개선 후: 데이터가 오래됐어도 /summary가 즉시 반환.

    주의: TestClient는 BackgroundTask를 응답 전에 동기 실행하므로
    _silent_refresh를 빠른 mock으로 대체해 응답 시간만 측정한다.
    실제 uvicorn에서는 응답 전송 후 백그라운드에서 수집이 실행된다.
    """
    _insert_stale_data(db_session, "005930", "삼성전자")

    refresh_called = []

    def instant_mock_refresh(stock_code):
        # 실제 수집은 백그라운드에서 진행됨 — 여기선 호출 여부만 확인
        refresh_called.append(stock_code)

    with patch("app.services.dart_service.DartService.resolve_stock_code", return_value="005930"), \
         patch("app.services.dart_service.DartService.get_sector_by_stock_code", return_value="전자"), \
         patch("app.api.v1.finance._silent_refresh", side_effect=instant_mock_refresh):

        t0 = time.perf_counter()
        r = client.get("/api/v1/finance/summary?keyword=삼성전자", headers=auth_headers)
        elapsed = time.perf_counter() - t0

    assert r.status_code == 200
    assert elapsed < 1.0, f"즉시 반환 실패: {elapsed:.3f}s"
    assert "005930" in refresh_called, "백그라운드 갱신이 예약되지 않음"

    print(f"""
┌─────────────────────────────────────────────────────────┐
│  [개선 후] 재조회 시나리오 (stale data)                   │
├─────────────────────────────────────────────────────────┤
│  /summary 사용자 응답          : {elapsed * 1000:6.1f}ms              │
│  백그라운드 재수집 예약         : ✓ (응답 후 ~{DART_COLLECTION_SECONDS}s 소요)    │
│  사용자 체감 대기 시간          : {elapsed * 1000:6.1f}ms              │
│                                                         │
│  * TestClient는 백그라운드 작업을 동기 실행하므로        │
│    실제 서버(uvicorn)에서만 비동기 효과가 나타남          │
└─────────────────────────────────────────────────────────┘""")


# ── 비교 요약 ──────────────────────────────────────────────────────────────────

def test_improvement_summary(client, db_session, auth_headers):
    """개선 전/후 응답 시간 비율 계산."""
    _insert_stale_data(db_session, "005930", "삼성전자")

    with patch("app.services.dart_service.DartService.resolve_stock_code", return_value="005930"), \
         patch("app.services.dart_service.DartService.get_sector_by_stock_code", return_value="전자"), \
         patch("app.api.v1.finance._silent_refresh"):

        times = []
        for _ in range(5):
            t = time.perf_counter()
            client.get("/api/v1/finance/summary?keyword=삼성전자", headers=auth_headers)
            times.append((time.perf_counter() - t) * 1000)

    avg_ms = sum(times) / len(times)
    before_ms = DART_COLLECTION_SECONDS * 1000
    speedup = before_ms / avg_ms

    print(f"""
╔═════════════════════════════════════════════╗
║          로딩 시간 개선 효과 요약             ║
╠═════════════════════════════════════════════╣
║  개선 전 (stale 재조회 대기)  : ~{DART_COLLECTION_SECONDS:>5}s        ║
║  개선 후 (/summary 평균)      : {avg_ms:>7.1f}ms      ║
║                                             ║
║  체감 속도 향상               :  약 {speedup:>4.0f}배    ║
╠═════════════════════════════════════════════╣
║  적용 시나리오                               ║
║  ✓ stale 재조회 (7일 초과)  → 즉시 반환     ║
║  ✓ 서버 재시작 후 인기 종목  → 사전 수집     ║
║  ✗ 첫 수집 (데이터 없음)    → 변화 없음     ║
╚═════════════════════════════════════════════╝""")

    assert avg_ms < 1000
