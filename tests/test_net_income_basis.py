"""Phase 106: net_income 지배주주 기준 통일 테스트.

CONCEPT_MAPPING이 연결총(net_income_total)/지배주주(net_income_owners)를 분리 저장하고,
_coalesce_net_income_basis가 지배주주 우선·연결총 폴백으로 net_income을 확정하는지 검증한다.
"""
import logging
from unittest.mock import MagicMock, patch

from app.services.dart_service import DartService, _coalesce_net_income_basis


def _coalesced(values: dict) -> dict:
    extracted = {2022: dict(values)}
    _coalesce_net_income_basis(extracted, [2022])
    return extracted[2022]


def test_net_income_prefers_owners_over_total():
    # T1: owners·total 둘 다 있으면 지배주주 채택 (카카오 2022형: total=1067, owners=1358)
    result = _coalesced({"net_income_total": 1067, "net_income_owners": 1358})
    assert result["net_income"] == 1358
    assert "net_income_total" not in result
    assert "net_income_owners" not in result


def test_net_income_prefers_owners_even_when_smaller():
    # 정상 케이스(비지배 흑자): owners < total 이어도 지배주주 우선 (Minor 보강)
    result = _coalesced({"net_income_total": 1000, "net_income_owners": 900})
    assert result["net_income"] == 900


def test_collect_financial_data_coalesces_owners_end_to_end():
    # 통합(Minor 보강): fast-path 실제 concept 매핑 → abs-max → coalesce 전 경로.
    # ProfitLoss(연결총)·OwnersOfParent(지배주주)가 둘 다 와도 net_income=지배주주,
    # 중간 키는 반환 dict에 남지 않는다.
    corp = MagicMock()
    corp.corp_code = "000000"
    rows = [
        {"account_id": "ifrs-full_ProfitLoss", "account_nm": "당기순이익",
         "thstrm_amount": "1067", "frmtrm_amount": "", "bfefrmtrm_amount": ""},
        {"account_id": "ifrs-full_ProfitLossAttributableToOwnersOfParent", "account_nm": "지배기업 소유주지분",
         "thstrm_amount": "1358", "frmtrm_amount": "", "bfefrmtrm_amount": ""},
    ]
    with patch.object(DartService, "_request_fnltt", return_value=rows):
        result = DartService._collect_financial_data(corp, DartService.CONCEPT_MAPPING, [2022])

    assert result[2022]["net_income"] == 1358
    assert "net_income_total" not in result[2022]
    assert "net_income_owners" not in result[2022]
    corp.extract_fs.assert_not_called()


def test_net_income_falls_back_to_total_when_owners_missing():
    # T2: owners 없으면 연결총 폴백 (NAVER/SK이노형: concept 미검출)
    result = _coalesced({"net_income_total": 1067})
    assert result["net_income"] == 1067
    assert "net_income_total" not in result


def test_net_income_concepts_are_split_before_abs_max_dedup():
    # T3: 두 concept가 분리 매핑돼야 abs-max가 서로 충돌하지 않음 (비금융·금융 동일)
    for mapping in (DartService.CONCEPT_MAPPING, DartService.FINANCIAL_CONCEPT_MAPPING):
        assert mapping["ifrs-full_ProfitLoss"] == "net_income_total"
        assert mapping["ifrs-full_ProfitLossAttributableToOwnersOfParent"] == "net_income_owners"


def test_owner_label_fallback_ignores_spaces_and_is_safe_subset():
    # T4: 공백 제거 후 '순이익' 접미사 키만 owners로 매핑. 자본총계와 겹치는 바 형태는 제외.
    account_nm = "지배주주지분 순이익"
    normalized = account_nm.replace(" ", "")
    assert DartService.GENERAL_ACCOUNT_NAME_MAPPING[normalized] == "net_income_owners"
    # 위험 키(자본 항목 라벨 충돌)는 매핑에 없어야 한다
    assert "지배기업소유주지분" not in DartService.GENERAL_ACCOUNT_NAME_MAPPING
    assert "지배기업의소유주지분" not in DartService.GENERAL_ACCOUNT_NAME_MAPPING


def test_net_income_missing_when_both_sources_missing():
    # T5: 둘 다 없으면 net_income 키 미생성 (결측 유지)
    result = _coalesced({"revenue": 100})
    assert "net_income" not in result


def test_net_income_keeps_negative_owner_value():
    # T6: 지배주주 적자면 net_income 음수 (에코프로비엠 2023형). 연결총이 흑자여도 지배주주 우선.
    result = _coalesced({"net_income_total": 100, "net_income_owners": -700})
    assert result["net_income"] == -700


def test_net_income_basis_logs_owners_of_parent(caplog):
    # Phase 107 (AI 최종 비준): 지배주주 채택 시 출처 기준을 로그로 남긴다.
    extracted = {2022: {"net_income_total": 1067, "net_income_owners": 1358}}
    with caplog.at_level(logging.INFO, logger="app.services.dart_service"):
        _coalesce_net_income_basis(extracted, [2022], "035720")
    assert "basis=owners_of_parent" in caplog.text
    assert "035720" in caplog.text


def test_net_income_basis_logs_consolidated_total_fallback(caplog):
    # Phase 107: 지배주주 결측으로 연결총 폴백 시 fallback 기준을 명시 로그.
    extracted = {2021: {"net_income_total": 16489}}
    with caplog.at_level(logging.INFO, logger="app.services.dart_service"):
        _coalesce_net_income_basis(extracted, [2021], "035420")
    assert "basis=consolidated_total_fallback" in caplog.text


def test_net_income_basis_no_log_when_both_missing(caplog):
    # 둘 다 결측이면 net_income 미확정 → 기준 로그도 남기지 않는다.
    extracted = {2020: {"revenue": 100}}
    with caplog.at_level(logging.INFO, logger="app.services.dart_service"):
        _coalesce_net_income_basis(extracted, [2020], "000000")
    assert "basis=" not in caplog.text
