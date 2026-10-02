"""Phase 98 — GET /disclosures (신호 기반 DART 공시 검색) 테스트."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.dart_service import DartService, CompanyNotFoundError

URL = "/api/v1/finance/disclosures"

_CORP = SimpleNamespace(corp_code="00126380", corp_name="삼성전자", stock_code="005930")

_SAMPLE_ROWS = [
    {"report_nm": "사업보고서 (2025.12)", "rcept_no": "20260311000123",
     "rcept_dt": "20260311", "flr_nm": "삼성전자"},
    {"report_nm": "연결재무제표기준영업(잠정)실적(공정공시)", "rcept_no": "20260128000456",
     "rcept_dt": "20260128", "flr_nm": "삼성전자"},
    {"report_nm": "주요사항보고서(유상증자결정)", "rcept_no": "20251215000789",
     "rcept_dt": "20251215", "flr_nm": "삼성전자"},
    # 손익 무관 공시 — 필터에서 제외돼야 함
    {"report_nm": "임원ㆍ주요주주특정증권등소유상황보고서", "rcept_no": "20251201000111",
     "rcept_dt": "20251201", "flr_nm": "홍길동"},
]


@pytest.fixture(autouse=True)
def reset_disclosure_cache():
    DartService._disclosure_cache.clear()
    yield
    DartService._disclosure_cache.clear()


def _get(client, headers, **params):
    query = {"stock_code": "005930", **params}
    with patch("app.services.dart_service.DartService.get_corp_by_stock_code", return_value=_CORP), \
         patch("app.services.dart_service.DartService.fetch_recent_disclosures", return_value=list(_SAMPLE_ROWS)):
        return client.get(URL, params=query, headers=headers)


# ── 엔드포인트 ────────────────────────────────────────────────────────────────

def test_disclosures_success(client, auth_headers):
    response = _get(client, auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["stock_code"] == "005930"
    assert body["company"] == "삼성전자"
    assert body["rule_id"] is None
    # rule_id 미지정 → periodic/performance/major 전부, 무관 공시(소유상황보고서)는 제외
    assert [d["category"] for d in body["disclosures"]] == ["periodic", "performance", "major"]
    first = body["disclosures"][0]
    assert first["title"] == "사업보고서 (2025.12)"
    assert first["date"] == "2026-03-11"
    assert first["viewer_url"] == "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260311000123"


def test_disclosures_warning_rule_includes_major(client, auth_headers):
    response = _get(client, auth_headers, rule_id="revenue-up-operating-profit-down")

    assert response.status_code == 200
    body = response.json()
    assert body["rule_id"] == "revenue-up-operating-profit-down"
    assert "major" in {d["category"] for d in body["disclosures"]}


def test_disclosures_positive_rule_excludes_major(client, auth_headers):
    response = _get(client, auth_headers, rule_id="revenue-and-operating-profit-up")

    assert response.status_code == 200
    categories = {d["category"] for d in response.json()["disclosures"]}
    assert categories == {"periodic", "performance"}


def test_disclosures_cash_flow_warning_includes_major(client, auth_headers):
    """Phase 99 — 현금흐름 warning rule은 주요사항보고서(major)를 포함한다."""
    response = _get(client, auth_headers, rule_id="net-income-positive-operating-cash-flow-negative")

    assert response.status_code == 200
    assert "major" in {d["category"] for d in response.json()["disclosures"]}


def test_disclosures_cash_flow_positive_excludes_major(client, auth_headers):
    """Phase 99 — 현금흐름 positive rule은 major를 제외한다."""
    response = _get(client, auth_headers, rule_id="operating-cash-flow-turnaround")

    assert response.status_code == 200
    categories = {d["category"] for d in response.json()["disclosures"]}
    assert categories == {"periodic", "performance"}


def test_disclosures_invalid_rule_id(client, auth_headers):
    response = client.get(
        URL, params={"stock_code": "005930", "rule_id": "unknown-rule"}, headers=auth_headers
    )

    assert response.status_code == 400
    assert "unknown-rule" in response.json()["detail"]


def test_disclosures_unknown_stock(client, auth_headers):
    with patch(
        "app.services.dart_service.DartService.get_corp_by_stock_code",
        side_effect=CompanyNotFoundError("해당 종목을 찾을 수 없습니다."),
    ):
        response = client.get(URL, params={"stock_code": "999999"}, headers=auth_headers)

    assert response.status_code == 404


def test_disclosures_requires_auth(client):
    response = client.get(URL, params={"stock_code": "005930"})

    assert response.status_code == 401


def test_disclosures_empty_result(client, auth_headers):
    with patch("app.services.dart_service.DartService.get_corp_by_stock_code", return_value=_CORP), \
         patch("app.services.dart_service.DartService.fetch_recent_disclosures", return_value=[]):
        response = client.get(URL, params={"stock_code": "005930"}, headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["disclosures"] == []


# ── fetch_recent_disclosures (서비스 함수) ────────────────────────────────────

def test_fetch_recent_disclosures_daily_cache():
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "000", "list": list(_SAMPLE_ROWS)}

    with patch("app.services.dart_service.requests.get", return_value=mock_resp) as mock_get:
        first = DartService.fetch_recent_disclosures("00126380")
        second = DartService.fetch_recent_disclosures("00126380")

    # 유형별(A/B/I) 3회 호출 후 병합·최신순 정렬. 캐시 히트 시 추가 호출 없음.
    assert [r["rcept_no"] for r in first] == [r["rcept_no"] for r in sorted(
        _SAMPLE_ROWS * 3, key=lambda r: r["rcept_dt"], reverse=True)]
    assert second is first
    assert mock_get.call_count == 3
    assert DartService._disclosure_cache["00126380"][0] == date.today()


def test_fetch_recent_disclosures_error_status_raises_and_not_cached():
    # 쿼터 초과(020) 등 비정상 status는 예외를 올리고 캐시에 남기지 않는다.
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "020", "message": "사용한도 초과"}

    with patch("app.services.dart_service.requests.get", return_value=mock_resp):
        with pytest.raises(RuntimeError, match="status=020"):
            DartService.fetch_recent_disclosures("00126380")

    assert "00126380" not in DartService._disclosure_cache


def test_fetch_recent_disclosures_status_013_returns_empty_and_cached():
    # 013(조회된 데이터 없음)은 정상 빈 결과 — 빈 리스트를 반환하고 당일 캐시에 저장한다.
    mock_resp = MagicMock()
    mock_resp.json.return_value = {"status": "013", "message": "조회된 데이타가 없습니다."}

    with patch("app.services.dart_service.requests.get", return_value=mock_resp) as mock_get:
        result = DartService.fetch_recent_disclosures("00126380")

    assert result == []
    assert mock_get.call_count == 3
    assert DartService._disclosure_cache["00126380"] == (date.today(), [])
