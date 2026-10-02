"""
DartService 유닛 테스트 (10개)

실제 DART API를 호출하지 않도록 _global_corp_list를 모킹한다.
"""
import pytest
from datetime import datetime
from unittest.mock import MagicMock, call, patch

from app.services.dart_service import CompanyNotFoundError, DartService, ServiceNotReadyError


@pytest.fixture
def dart_service_state():
    saved = {
        "_global_corp_list": DartService._global_corp_list,
        "_init_failed": DartService._init_failed,
        "_krx_degraded": DartService._krx_degraded,
        "_krx_checked": DartService._krx_checked,
        "_krx_listed_codes": DartService._krx_listed_codes,
        "_induty_code_cache": DartService._induty_code_cache,
        "_stock_corps": DartService._stock_corps,
        "_name_index": DartService._name_index,
    }
    DartService._global_corp_list = None
    DartService._init_failed = False
    DartService._krx_degraded = False
    DartService._krx_checked = False
    DartService._krx_listed_codes = None
    DartService._induty_code_cache = {}
    DartService._stock_corps = []
    DartService._name_index = {}
    try:
        yield
    finally:
        DartService._global_corp_list = saved["_global_corp_list"]
        DartService._init_failed = saved["_init_failed"]
        DartService._krx_degraded = saved["_krx_degraded"]
        DartService._krx_checked = saved["_krx_checked"]
        DartService._krx_listed_codes = saved["_krx_listed_codes"]
        DartService._induty_code_cache = saved["_induty_code_cache"]
        DartService._stock_corps = saved["_stock_corps"]
        DartService._name_index = saved["_name_index"]


# ── COMPANY_ALIASES (alias 매핑) ─────────────────────────────────────────────

def test_resolve_alias_naver():
    """'네이버' alias가 NAVER 종목코드(035420)로 변환된다."""
    mock_corp_list = MagicMock()
    with patch.object(DartService, "_global_corp_list", mock_corp_list):
        result = DartService.resolve_stock_code("네이버")
    assert result == "035420"


def test_resolve_alias_case_insensitive():
    """alias는 대소문자를 구분하지 않는다 ('NAVER' → 035420)."""
    mock_corp_list = MagicMock()
    with patch.object(DartService, "_global_corp_list", mock_corp_list):
        result = DartService.resolve_stock_code("NAVER")
    assert result == "035420"


def test_resolve_alias_nc():
    """'엔씨', 'NC' 등 여러 alias가 동일 종목코드(036570)로 변환된다."""
    mock_corp_list = MagicMock()
    with patch.object(DartService, "_global_corp_list", mock_corp_list):
        assert DartService.resolve_stock_code("엔씨") == "036570"
        assert DartService.resolve_stock_code("NC") == "036570"
        assert DartService.resolve_stock_code("nc소프트") == "036570"


def test_search_companies_alias_inserts_at_top():
    """alias 매칭 기업이 corp_name 검색 결과에 없을 때 최상단에 삽입된다."""
    def make_corp(name, code):
        c = MagicMock()
        c.corp_name = name
        c.stock_code = code
        return c

    # DART corp_name은 "NAVER"이므로 "네이버" 검색 시 substring 매칭 안 됨
    stock_corps = [make_corp("NAVER", "035420"), make_corp("다른기업", "000001")]
    mock_corp_list = MagicMock()

    with patch.object(DartService, "_global_corp_list", mock_corp_list), \
         patch.object(DartService, "_stock_corps", stock_corps):
        results = DartService.search_companies("네이버")

    assert len(results) >= 1
    assert results[0].stock_code == "035420"


def test_search_companies_alias_not_duplicated():
    """alias 매칭 기업이 이미 결과에 있으면 중복 삽입하지 않는다."""
    def make_corp(name, code):
        c = MagicMock()
        c.corp_name = name
        c.stock_code = code
        return c

    # "삼성" 검색 시 "삼성전자"가 substring으로 이미 포함됨
    stock_corps = [make_corp("삼성전자", "005930"), make_corp("삼성SDI", "006400")]
    mock_corp_list = MagicMock()

    with patch.object(DartService, "_global_corp_list", mock_corp_list), \
         patch.object(DartService, "_stock_corps", stock_corps):
        results = DartService.search_companies("삼성")

    codes = [c.stock_code for c in results]
    assert codes.count("005930") == 1  # 중복 없음


def test_search_companies_alias_existing_result_moves_to_top():
    """alias 매칭 기업이 결과 안에 있으면 중복 없이 최상단으로 이동한다."""
    def make_corp(name, code):
        c = MagicMock()
        c.corp_name = name
        c.stock_code = code
        return c

    stock_corps = [
        make_corp("SK", "003600"),
        make_corp("SK하이닉스", "000660"),
        make_corp("SK", "034730"),
    ]
    mock_corp_list = MagicMock()

    with patch.object(DartService, "_global_corp_list", mock_corp_list), \
         patch.object(DartService, "_stock_corps", stock_corps):
        results = DartService.search_companies("SK")

    codes = [c.stock_code for c in results]
    assert results[0].stock_code == "034730"
    assert codes.count("034730") == 1


def test_normalize_alias_key_removes_special_chars():
    """alias 정규화가 공백, &, -, .을 제거한다."""
    assert DartService._normalize_alias_key("KT&G") == "ktg"
    assert DartService._normalize_alias_key("kt & g") == "ktg"
    assert DartService._normalize_alias_key("S-Oil") == "soil"
    assert DartService._normalize_alias_key("LG H&H") == "lghh"
    assert DartService._normalize_alias_key("F&F") == "ff"
    assert DartService._normalize_alias_key("DL E&C") == "dlec"


def test_resolve_alias_kt_and_g_variants():
    """KT&G 변형 alias가 모두 동일 종목코드로 변환된다."""
    mock_corp_list = MagicMock()
    with patch.object(DartService, "_global_corp_list", mock_corp_list):
        for query in ["KT&G", "kt & g", "ktng", "케이티앤지"]:
            assert DartService.resolve_stock_code(query) == "033780"


def test_resolve_alias_short_holding_companies():
    """짧은 영문 alias는 지주사 또는 모기업 기준으로 매핑된다."""
    mock_corp_list = MagicMock()
    with patch.object(DartService, "_global_corp_list", mock_corp_list):
        assert DartService.resolve_stock_code("SK") == "034730"
        assert DartService.resolve_stock_code("sk") == "034730"
        assert DartService.resolve_stock_code("LS") == "006260"
        assert DartService.resolve_stock_code("GS") == "078930"
        assert DartService.resolve_stock_code("CJ") == "001040"
        assert DartService.resolve_stock_code("KT") == "030200"
        assert DartService.resolve_stock_code("SKC") == "011790"
        assert DartService.resolve_stock_code("NC") == "036570"


def test_resolve_alias_english_company_names():
    """대표 영문 사명 alias가 정상 매칭된다."""
    mock_corp_list = MagicMock()
    with patch.object(DartService, "_global_corp_list", mock_corp_list):
        assert DartService.resolve_stock_code("posco holdings") == "005490"
        assert DartService.resolve_stock_code("samsung sdi") == "006400"
        assert DartService.resolve_stock_code("hd hyundai") == "267250"
        assert DartService.resolve_stock_code("lg display") == "034220"
        assert DartService.resolve_stock_code("S-Oil") == "010950"


def test_resolve_alias_hd_construction_equipment_variants():
    """HD건설기계의 한국어 변형(HD현대건설기계)도 매칭된다."""
    mock_corp_list = MagicMock()
    with patch.object(DartService, "_global_corp_list", mock_corp_list):
        assert DartService.resolve_stock_code("HD건설기계") == "267270"
        assert DartService.resolve_stock_code("HD현대건설기계") == "267270"
        assert DartService.resolve_stock_code("현대건설기계") == "267270"


def test_resolve_alias_existing_unchanged():
    """기존 alias 매핑은 정규화 규칙 변경 후에도 동일하게 동작한다."""
    mock_corp_list = MagicMock()
    with patch.object(DartService, "_global_corp_list", mock_corp_list):
        assert DartService.resolve_stock_code("네이버") == "035420"
        assert DartService.resolve_stock_code("NAVER") == "035420"
        assert DartService.resolve_stock_code("엔씨") == "036570"
        assert DartService.resolve_stock_code("samsung") == "005930"
        assert DartService.resolve_stock_code("기아차") == "000270"


def test_search_companies_uses_normalized_key():
    """search_companies도 특수문자 제거 후 alias 매칭 결과를 최상단에 삽입한다."""
    def make_corp(name, code):
        c = MagicMock()
        c.corp_name = name
        c.stock_code = code
        return c

    stock_corps = [make_corp("KT&G", "033780"), make_corp("다른기업", "000001")]
    mock_corp_list = MagicMock()

    with patch.object(DartService, "_global_corp_list", mock_corp_list), \
         patch.object(DartService, "_stock_corps", stock_corps):
        results = DartService.search_companies("kt & g")

    assert results[0].stock_code == "033780"


def test_resolve_stock_code_six_digits_passthrough():
    """6자리 숫자 코드는 corp_list 탐색 없이 그대로 반환된다."""
    mock_corp_list = MagicMock()
    with patch.object(DartService, "_global_corp_list", mock_corp_list):
        result = DartService.resolve_stock_code("005930")
    assert result == "005930"


def test_resolve_stock_code_raises_when_not_initialized():
    """corp_list 미초기화 상태에서 기업명 조회 시 ServiceNotReadyError가 발생한다."""
    with patch.object(DartService, "_global_corp_list", None):
        with pytest.raises(ServiceNotReadyError):
            DartService.resolve_stock_code("삼성전자")


def test_search_companies_raises_when_not_initialized():
    """corp_list 미초기화 상태에서 검색 시 ServiceNotReadyError가 발생한다."""
    with patch.object(DartService, "_global_corp_list", None):
        with pytest.raises(ServiceNotReadyError):
            DartService.search_companies("삼성")


def test_initialize_retries_then_succeeds(monkeypatch, dart_service_state):
    corp = MagicMock()
    corp.corp_name = "RetrySuccess"
    corp.stock_code = "000001"
    corp_list = MagicMock()
    corp_list.corps = [corp]
    calls = {"count": 0}

    def fake_get_corp_list():
        calls["count"] += 1
        if calls["count"] == 1:
            raise ConnectionError("temporary failure")
        return corp_list

    monkeypatch.setattr("app.services.dart_service.dart.get_corp_list", fake_get_corp_list)
    monkeypatch.setattr("app.services.dart_service.dart.set_api_key", lambda api_key: None)
    monkeypatch.setattr("app.services.dart_service.settings.KRX_API_KEY", "")
    monkeypatch.setattr("app.services.dart_service.time.sleep", lambda delay: None)

    DartService.initialize(_retry_delays=(0,))

    assert calls["count"] == 2
    assert DartService._global_corp_list is corp_list
    assert DartService._init_failed is False
    assert DartService._stock_corps == [corp]
    assert DartService._name_index == {"RetrySuccess": "000001"}


def test_initialize_marks_krx_degraded_when_samsung_probe_is_missing(
    monkeypatch, dart_service_state, caplog
):
    corp_list = MagicMock()
    corp_list.corps = []
    corp_list.find_by_stock_code.return_value = None
    monkeypatch.setattr("app.services.dart_service.dart.get_corp_list", lambda: corp_list)
    monkeypatch.setattr("app.services.dart_service.dart.set_api_key", lambda api_key: None)
    monkeypatch.setattr("app.services.dart_service.settings.KRX_API_KEY", "")

    DartService.initialize(_retry_delays=())

    assert DartService._krx_degraded is True
    assert DartService._krx_checked is True
    assert "삼성전자(005930)를 상장 인덱스에서 찾지 못했습니다" in caplog.text


def test_initialize_marks_krx_ok_when_samsung_probe_succeeds(
    monkeypatch, dart_service_state
):
    samsung = MagicMock(corp_name="삼성전자", stock_code="005930")
    corp_list = MagicMock()
    corp_list.corps = [samsung]
    corp_list.find_by_stock_code.return_value = samsung
    monkeypatch.setattr("app.services.dart_service.dart.get_corp_list", lambda: corp_list)
    monkeypatch.setattr("app.services.dart_service.dart.set_api_key", lambda api_key: None)
    monkeypatch.setattr("app.services.dart_service.settings.KRX_API_KEY", "")

    DartService.initialize(_retry_delays=())

    assert DartService._krx_degraded is False
    assert DartService._krx_checked is True


def test_initialize_marks_krx_ok_when_official_list_succeeds(
    monkeypatch, dart_service_state
):
    corp_list = MagicMock()
    corp_list.corps = []
    corp_list.find_by_stock_code.return_value = None
    monkeypatch.setattr("app.services.dart_service.dart.get_corp_list", lambda: corp_list)
    monkeypatch.setattr("app.services.dart_service.dart.set_api_key", lambda api_key: None)
    def fake_fetch_krx(cls):
        cls._krx_listed_codes = {"005930"}
        return cls._krx_listed_codes

    monkeypatch.setattr(DartService, "_fetch_krx_listed_codes", classmethod(fake_fetch_krx))

    DartService.initialize(_retry_delays=())

    assert DartService._krx_degraded is False
    assert DartService._krx_checked is True
    assert DartService._krx_listed_codes == {"005930"}


def test_initialize_marks_failed_after_exhausting_retries(monkeypatch, dart_service_state):
    calls = {"count": 0}

    def fake_get_corp_list():
        calls["count"] += 1
        raise ConnectionError("permanent failure")

    monkeypatch.setattr("app.services.dart_service.dart.get_corp_list", fake_get_corp_list)
    monkeypatch.setattr("app.services.dart_service.dart.set_api_key", lambda api_key: None)
    monkeypatch.setattr("app.services.dart_service.time.sleep", lambda delay: None)

    DartService.initialize(_retry_delays=())

    assert calls["count"] == 1
    assert DartService._init_failed is True
    assert DartService._global_corp_list is None
    assert DartService._stock_corps == []
    assert DartService._name_index == {}


def test_resolve_stock_code_reports_failed_state(dart_service_state):
    DartService._global_corp_list = None
    DartService._init_failed = True

    with pytest.raises(ServiceNotReadyError) as exc_info:
        DartService.resolve_stock_code("Samsung")

    assert "초기화에 실패" in str(exc_info.value)


def test_resolve_stock_code_reports_loading_state(dart_service_state):
    DartService._global_corp_list = None
    DartService._init_failed = False

    with pytest.raises(ServiceNotReadyError) as exc_info:
        DartService.resolve_stock_code("Samsung")

    assert "로딩 중" in str(exc_info.value)


def test_search_companies_reports_failed_state(dart_service_state):
    DartService._global_corp_list = None
    DartService._init_failed = True

    with pytest.raises(ServiceNotReadyError) as exc_info:
        DartService.search_companies("Samsung")

    assert "초기화에 실패" in str(exc_info.value)


class _SyncThread:
    """threading.Thread 대체 — start() 시 target을 즉시 동기 실행(테스트 결정성)."""

    def __init__(self, target=None, daemon=None, name=None):
        self._target = target

    def start(self):
        if self._target:
            self._target()


def test_trigger_reinitialize_already_ready(dart_service_state):
    DartService._global_corp_list = MagicMock()
    assert DartService.trigger_reinitialize() == "already_ready"


def test_trigger_reinitialize_loading(dart_service_state):
    DartService._global_corp_list = None
    DartService._init_failed = False
    assert DartService.trigger_reinitialize() == "loading"


def test_trigger_reinitialize_starts_when_failed(monkeypatch, dart_service_state):
    DartService._global_corp_list = None
    DartService._init_failed = True
    init_calls = {"count": 0}

    def fake_init(cls, *args, **kwargs):
        init_calls["count"] += 1
        cls._global_corp_list = MagicMock()
        cls._init_failed = False

    monkeypatch.setattr(DartService, "initialize", classmethod(fake_init))
    monkeypatch.setattr("app.services.dart_service.threading.Thread", _SyncThread)

    result = DartService.trigger_reinitialize()

    assert result == "reinitializing"
    assert init_calls["count"] == 1
    # 락이 정상 반환되어 다시 획득 가능해야 한다 (누수 방지 확인).
    assert DartService._reinit_lock.acquire(blocking=False) is True
    DartService._reinit_lock.release()


def test_trigger_reinitialize_already_running(dart_service_state):
    DartService._global_corp_list = None
    DartService._init_failed = True
    assert DartService._reinit_lock.acquire(blocking=False) is True
    try:
        assert DartService.trigger_reinitialize() == "already_running"
    finally:
        DartService._reinit_lock.release()


def test_search_companies_returns_at_most_10():
    """검색 결과는 최대 10개만 반환된다 (15개 후보 중 10개 제한)."""
    def make_corp(i):
        c = MagicMock()
        c.corp_name = f"테스트기업{i:02d}"
        c.stock_code = f"{i:06d}"
        return c

    mock_corp_list = MagicMock()
    mock_corp_list.corps = [make_corp(i) for i in range(15)]

    with patch.object(DartService, "_global_corp_list", mock_corp_list):
        results = DartService.search_companies("테스트기업")

    assert len(results) <= 10


def test_latest_fiscal_year_after_march_uses_previous_year():
    assert DartService._latest_fiscal_year(datetime(2026, 4, 1)) == 2025


def test_latest_fiscal_year_before_april_uses_two_years_ago():
    assert DartService._latest_fiscal_year(datetime(2026, 3, 31)) == 2024


def test_financial_statement_model_has_phase90_columns():
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.pool import StaticPool

    from app.db.database import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)

    columns = {col["name"] for col in inspect(engine).get_columns("Financial_Statement")}
    assert {
        "net_interest_income",
        "loan_loss_provision",
        "insurance_liability",
        "sector_detail",
    }.issubset(columns)


def test_collect_financial_data_reraises_recent_report_failure():
    """fast-path와 extract_fs 폴백이 모두 실패해 데이터가 전무하면 상위 retry loop로 전파한다."""
    corp = MagicMock()
    corp.extract_fs.side_effect = RuntimeError("DART timeout")

    # fast-path는 아무것도 못 채움 → extract_fs 폴백이 RuntimeError → 데이터 전무 → 재전파
    with patch.object(DartService, "_collect_via_open_api", side_effect=lambda *a, **k: None):
        with pytest.raises(RuntimeError):
            DartService._collect_financial_data(corp, DartService.CONCEPT_MAPPING, [2021, 2022])


# ── Phase 77: fnlttSinglAcntAll fast-path ────────────────────────────────────

def test_collect_via_open_api_parses_multiyear():
    """fnlttSinglAcntAll 응답을 5개년으로 파싱 — 음수·빈값·원단위(환산 없음)·비표준 무시."""
    rows_2024 = [
        {"account_id": "ifrs-full_Revenue",
         "thstrm_amount": "1000000", "frmtrm_amount": "(500000)", "bfefrmtrm_amount": ""},
        {"account_id": "ifrs-full_Assets",
         "thstrm_amount": "9000000", "frmtrm_amount": "8000000", "bfefrmtrm_amount": "7000000"},
        {"account_id": "-표준계정코드 미사용-",
         "thstrm_amount": "123", "frmtrm_amount": "123", "bfefrmtrm_amount": "123"},
    ]
    rows_2022 = [
        {"account_id": "ifrs-full_Assets",
         "thstrm_amount": "7000000", "frmtrm_amount": "6000000", "bfefrmtrm_amount": "5000000"},
    ]
    corp = MagicMock()
    corp.corp_code = "00126380"
    years = [2020, 2021, 2022, 2023, 2024]
    extracted = {y: {} for y in years}

    def fake_request(corp_code, bsns_year, fs_div):
        if fs_div != "CFS":
            return []
        if bsns_year == 2024:
            return rows_2024
        if bsns_year == 2022:
            return rows_2022
        return []

    with patch.object(DartService, "_request_fnltt", side_effect=fake_request):
        DartService._collect_via_open_api(corp, DartService.CONCEPT_MAPPING, years, extracted)

    assert extracted[2024]["revenue"] == 1000000        # 원 단위 그대로, 환산 없음
    assert extracted[2023]["revenue"] == -500000        # frmtrm 괄호 음수
    assert extracted[2024]["total_assets"] == 9000000
    assert extracted[2023]["total_assets"] == 8000000
    assert extracted[2022]["total_assets"] == 7000000   # bfefrmtrm
    assert extracted[2021]["total_assets"] == 6000000   # 2022 보고서 frmtrm
    assert extracted[2020]["total_assets"] == 5000000   # 2022 보고서 bfefrmtrm
    assert "revenue" not in extracted[2022]             # 빈 값은 미생성
    assert "ifrs-full" not in str(extracted[2024])      # 비표준/미매핑 account_id 무시


def test_fetch_open_api_year_cfs_falls_back_to_ofs():
    """CFS 무자료(빈 list)면 OFS로 폴백 호출한다."""
    calls = []

    def fake_request(corp_code, bsns_year, fs_div):
        calls.append(fs_div)
        if fs_div == "CFS":
            return []
        return [{"account_id": "ifrs-full_Assets",
                 "thstrm_amount": "100", "frmtrm_amount": "", "bfefrmtrm_amount": ""}]

    years = [2023, 2024]
    extracted = {y: {} for y in years}
    with patch.object(DartService, "_request_fnltt", side_effect=fake_request):
        DartService._fetch_open_api_year("00126380", 2024, DartService.CONCEPT_MAPPING, years, extracted)

    assert calls == ["CFS", "OFS"]
    assert extracted[2024]["total_assets"] == 100


def test_fetch_open_api_year_ignores_account_name_without_name_mapping():
    rows = [
        {"account_id": "unknown", "account_nm": "순이자이익", "thstrm_amount": "100"},
        {"account_id": "ifrs-full_Assets", "account_nm": "자산", "thstrm_amount": "200"},
    ]
    years = [2024]
    extracted = {2024: {}}

    with patch.object(DartService, "_request_fnltt", return_value=rows):
        DartService._fetch_open_api_year("00126380", 2024, DartService.CONCEPT_MAPPING, years, extracted)

    assert "net_interest_income" not in extracted[2024]
    assert extracted[2024]["total_assets"] == 200


def test_fetch_open_api_year_maps_financial_account_name():
    rows = [
        {
            "account_id": "unknown",
            "account_nm": "순 이자 이익",
            "thstrm_amount": "100",
            "frmtrm_amount": "90",
            "bfefrmtrm_amount": "",
        },
    ]
    years = [2023, 2024]
    extracted = {y: {} for y in years}

    with patch.object(DartService, "_request_fnltt", return_value=rows):
        DartService._fetch_open_api_year(
            "00126380",
            2024,
            DartService.FINANCIAL_CONCEPT_MAPPING,
            years,
            extracted,
            DartService.FINANCIAL_ACCOUNT_NAME_MAPPING,
        )

    assert extracted[2024]["net_interest_income"] == 100
    assert extracted[2023]["net_interest_income"] == 90


def test_fetch_open_api_year_maps_phase96_concepts():
    """Phase 96 신규 concept_id(금융수익·법인세비용·영업활동현금흐름)가 매핑된다."""
    rows = [
        {"account_id": "ifrs-full_FinanceIncome", "thstrm_amount": "100"},
        {"account_id": "ifrs-full_FinanceCosts", "thstrm_amount": "80"},
        {"account_id": "ifrs-full_ProfitLossBeforeTax", "thstrm_amount": "500"},
        {"account_id": "ifrs-full_IncomeTaxExpenseContinuingOperations", "thstrm_amount": "120"},
        {"account_id": "ifrs-full_CashFlowsFromUsedInOperatingActivities", "thstrm_amount": "700"},
        {"account_id": "ifrs-full_OtherIncome", "thstrm_amount": "30"},
        {"account_id": "dart_OtherLosses", "thstrm_amount": "20"},
    ]
    extracted = {2024: {}}

    with patch.object(DartService, "_request_fnltt", return_value=rows):
        DartService._fetch_open_api_year("00126380", 2024, DartService.CONCEPT_MAPPING, [2024], extracted)

    assert extracted[2024]["finance_income"] == 100
    assert extracted[2024]["finance_cost"] == 80
    assert extracted[2024]["income_before_tax"] == 500
    assert extracted[2024]["income_tax_expense"] == 120
    assert extracted[2024]["operating_cash_flow"] == 700
    assert extracted[2024]["other_income"] == 30
    assert extracted[2024]["other_expense"] == 20


def test_fetch_open_api_year_maps_phase96_korean_names():
    """concept_id 미매핑이어도 한글 계정명(공백 포함)으로 Phase 96 필드가 매핑된다."""
    rows = [
        {"account_id": "unknown", "account_nm": "금융 수익", "thstrm_amount": "100"},
        {"account_id": "unknown", "account_nm": "영업활동으로 인한 현금흐름", "thstrm_amount": "700"},
        {"account_id": "unknown", "account_nm": "법인세비용차감전순이익", "thstrm_amount": "500"},
    ]
    extracted = {2024: {}}

    with patch.object(DartService, "_request_fnltt", return_value=rows):
        DartService._fetch_open_api_year(
            "00126380",
            2024,
            DartService.CONCEPT_MAPPING,
            [2024],
            extracted,
            DartService.GENERAL_ACCOUNT_NAME_MAPPING,
        )

    assert extracted[2024]["finance_income"] == 100
    assert extracted[2024]["operating_cash_flow"] == 700
    assert extracted[2024]["income_before_tax"] == 500


def test_parse_fs_into_maps_financial_account_name_label():
    import pandas as pd

    fs = {
        "is": pd.DataFrame([{"label_ko": "보험 계약 부채", "2024": "123"}]),
        "cis": pd.DataFrame(),
        "bs": pd.DataFrame(),
        "cbs": pd.DataFrame(),
    }
    extracted = {2024: {}}

    DartService._parse_fs_into(
        fs,
        [2024],
        DartService.FINANCIAL_CONCEPT_MAPPING,
        extracted,
        DartService.FINANCIAL_ACCOUNT_NAME_MAPPING,
    )

    assert extracted[2024]["insurance_liability"] == 123


def test_collect_financial_data_uses_open_api_first():
    """fast-path가 모든 연도를 채우면 extract_fs는 호출되지 않는다."""
    corp = MagicMock()

    def fake_via(corp_, mapping, years, extracted):
        for y in years:
            extracted[y]["revenue"] = 100

    with patch.object(DartService, "_collect_via_open_api", side_effect=fake_via):
        result = DartService._collect_financial_data(corp, DartService.CONCEPT_MAPPING, [2023, 2024])

    assert result[2024]["revenue"] == 100
    corp.extract_fs.assert_not_called()


def test_collect_financial_data_falls_back_to_extract_fs():
    """fast-path가 비면 extract_fs 폴백으로 데이터를 채운다."""
    corp = MagicMock()

    def fake_parse(fs, years, mapping, extracted):
        for y in years:
            extracted[y] = {"revenue": 777}

    with patch.object(DartService, "_collect_via_open_api", side_effect=lambda *a, **k: None), \
         patch.object(DartService, "_parse_fs_into", side_effect=fake_parse):
        result = DartService._collect_financial_data(corp, DartService.CONCEPT_MAPPING, [2023, 2024])

    assert corp.extract_fs.call_count == 1               # 최신 보고서 1회로 전 연도 충족
    assert result[2024]["revenue"] == 777


# ── _extract_unit_from_column (3개) ──────────────────────────────────────────

def test_extract_unit_from_column_천원():
    assert DartService._extract_unit_from_column("2023(단위:천원)") == "천원"


def test_extract_unit_from_column_백만원():
    assert DartService._extract_unit_from_column("2022(단위:백만원)") == "백만원"


def test_extract_unit_from_column_no_unit_defaults_to_원():
    """단위 표기가 없는 컬럼은 '원'으로 간주한다."""
    assert DartService._extract_unit_from_column("2021") == "원"


# ── _normalize_to_krw (3개) ──────────────────────────────────────────────────

def test_normalize_to_krw_원_no_change():
    assert DartService._normalize_to_krw(1_000, "원") == 1_000


def test_normalize_to_krw_천원():
    assert DartService._normalize_to_krw(1_000, "천원") == 1_000_000


def test_normalize_to_krw_백만원():
    assert DartService._normalize_to_krw(1, "백만원") == 1_000_000


# ── _is_financial_sector (2개) ───────────────────────────────────────────────

def test_is_financial_sector_returns_true_for_bank():
    """실제 DART sector값 '기타 금융업'(은행·금융지주)은 금융업으로 판단한다."""
    mock_corp = MagicMock()
    mock_corp.corp_name = "KB국민은행"
    mock_corp.sector = "기타 금융업"   # 실환경 검증값 (dart-fss 0.4.15)

    with patch.object(DartService, "_global_corp_list", MagicMock()):
        result = DartService._is_financial_sector(mock_corp)

    assert result is True


def test_is_financial_sector_returns_true_for_insurance():
    """실제 DART sector값 '보험업'은 금융업으로 판단한다."""
    mock_corp = MagicMock()
    mock_corp.corp_name = "삼성화재"
    mock_corp.sector = "보험업"   # 실환경 검증값

    with patch.object(DartService, "_global_corp_list", MagicMock()):
        result = DartService._is_financial_sector(mock_corp)

    assert result is True


def test_is_financial_sector_returns_false_for_general():
    """일반 제조업 sector는 False를 반환한다."""
    mock_corp = MagicMock()
    mock_corp.corp_name = "삼성전자"
    mock_corp.sector = "반도체 제조업"   # 실환경 검증값

    with patch.object(DartService, "_global_corp_list", MagicMock()):
        result = DartService._is_financial_sector(mock_corp)

    assert result is False


def test_is_financial_sector_with_krx_sector_does_not_call_dart():
    corp = MagicMock(corp_code="00126380", corp_name="삼성전자", sector="반도체 제조업")

    with patch.object(DartService, "_fetch_induty_code") as fetch_induty:
        assert DartService._is_financial_sector(corp) is False

    fetch_induty.assert_not_called()


def test_is_financial_sector_falls_back_to_dart_for_kb_financial(dart_service_state):
    corp = MagicMock(corp_code="00688996", corp_name="KB금융", sector=None, _sector=None)
    response = MagicMock()
    response.json.return_value = {"status": "000", "induty_code": "64992"}

    with patch("app.services.dart_service.requests.get", return_value=response):
        assert DartService._is_financial_sector(corp) is True


def test_is_financial_sector_falls_back_to_dart_for_general_company(dart_service_state):
    corp = MagicMock(corp_code="00126380", corp_name="삼성전자", sector=None, _sector=None)
    response = MagicMock()
    response.json.return_value = {"status": "000", "induty_code": "264"}

    with patch("app.services.dart_service.requests.get", return_value=response):
        assert DartService._is_financial_sector(corp) is False


@pytest.mark.parametrize("induty_code", ["65", "66"])
def test_is_financial_sector_accepts_finance_related_ksic_codes(
    induty_code, dart_service_state
):
    corp = MagicMock(corp_code=f"corp-{induty_code}", corp_name="금융사", sector=None, _sector=None)
    response = MagicMock()
    response.json.return_value = {"status": "000", "induty_code": induty_code}

    with patch("app.services.dart_service.requests.get", return_value=response):
        assert DartService._is_financial_sector(corp) is True


def test_is_financial_sector_dart_failure_returns_false_and_warns(
    dart_service_state, caplog
):
    corp = MagicMock(corp_code="broken", corp_name="조회실패", sector=None, _sector=None)

    with patch("app.services.dart_service.requests.get", side_effect=ConnectionError("down")):
        assert DartService._is_financial_sector(corp) is False

    assert "DART 업종 코드 조회 실패 [broken]" in caplog.text


def test_is_financial_sector_empty_dart_response_returns_false_and_warns(
    dart_service_state, caplog
):
    corp = MagicMock(corp_code="empty", corp_name="빈응답", sector=None, _sector=None)
    response = MagicMock()
    response.json.return_value = {"status": "000"}

    with patch("app.services.dart_service.requests.get", return_value=response):
        assert DartService._is_financial_sector(corp) is False

    assert "DART 업종 코드 조회 실패 [empty]" in caplog.text


def test_induty_code_is_cached_by_corp_code(dart_service_state):
    corp = MagicMock(corp_code="cached", corp_name="캐시기업", sector=None, _sector=None)
    response = MagicMock()
    response.json.return_value = {"status": "000", "induty_code": "64992"}

    with patch("app.services.dart_service.requests.get", return_value=response) as get:
        assert DartService._is_financial_sector(corp) is True
        assert DartService._is_financial_sector(corp) is True

    get.assert_called_once()


def test_induty_code_failure_is_not_cached(dart_service_state):
    with patch(
        "app.services.dart_service.requests.get", side_effect=ConnectionError("down")
    ) as get:
        assert DartService._fetch_induty_code("retry") is None
        assert DartService._fetch_induty_code("retry") is None

    assert get.call_count == 2


def test_induty_code_recovers_after_failure(dart_service_state):
    corp = MagicMock(corp_code="recover", corp_name="금융사", sector=None, _sector=None)
    response = MagicMock()
    response.json.return_value = {"status": "000", "induty_code": "64992"}

    with patch(
        "app.services.dart_service.requests.get",
        side_effect=[ConnectionError("temporary"), response],
    ) as get:
        assert DartService._is_financial_sector(corp) is False
        assert DartService._is_financial_sector(corp) is True

    assert get.call_count == 2


# ── KRX listed-code set (Phase 136) ──────────────────────────────────────────

def _krx_response(*codes):
    response = MagicMock()
    response.json.return_value = {
        "OutBlock_1": [{"ISU_SRT_CD": code} for code in codes]
    }
    return response


def test_krx_listed_codes_skips_http_without_api_key(dart_service_state):
    with patch("app.services.dart_service.settings.KRX_API_KEY", ""), \
         patch("app.services.dart_service.requests.get") as get:
        assert DartService._fetch_krx_listed_codes() is None
    get.assert_not_called()


def test_krx_listed_codes_returns_union_of_three_markets(dart_service_state):
    responses = [_krx_response("005930"), _krx_response("035720"), _krx_response("123456")]
    with patch("app.services.dart_service.settings.KRX_API_KEY", "key"), \
         patch("app.services.dart_service.requests.get", side_effect=responses) as get:
        result = DartService._fetch_krx_listed_codes()

    assert result == {"005930", "035720", "123456"}
    assert get.call_count == 3
    assert all(call.kwargs["headers"] == {"AUTH_KEY": "key"} for call in get.call_args_list)
    assert all(call.kwargs["timeout"] == 10 for call in get.call_args_list)


def test_krx_listed_codes_returns_none_on_market_failure(dart_service_state, caplog):
    with patch("app.services.dart_service.settings.KRX_API_KEY", "key"), \
         patch("app.services.dart_service.requests.get", side_effect=[
             _krx_response("005930"), ConnectionError("KOSDAQ down")
         ]):
        assert DartService._fetch_krx_listed_codes() is None

    assert DartService._krx_listed_codes is None
    assert "KRX 상장 종목 조회 실패" in caplog.text


def test_krx_listed_codes_retries_empty_holiday_date(dart_service_state):
    empty = _krx_response()
    responses = [empty, empty, empty, _krx_response("005930"), _krx_response("035720"), _krx_response("123456")]
    with patch("app.services.dart_service.settings.KRX_API_KEY", "key"), \
         patch("app.services.dart_service.requests.get", side_effect=responses) as get:
        result = DartService._fetch_krx_listed_codes()

    assert result == {"005930", "035720", "123456"}
    assert get.call_count == 6
    assert get.call_args_list[0].kwargs["params"] != get.call_args_list[3].kwargs["params"]


def test_krx_listed_codes_returns_none_after_seven_empty_days(dart_service_state):
    with patch("app.services.dart_service.settings.KRX_API_KEY", "key"), \
         patch("app.services.dart_service.requests.get", side_effect=[_krx_response()] * 21) as get:
        assert DartService._fetch_krx_listed_codes() is None
    assert get.call_count == 7


@pytest.mark.parametrize("listed_codes,expected", [
    (None, None), ({"005930"}, True), ({"000660"}, False),
])
def test_is_listed_returns_tristate(dart_service_state, listed_codes, expected):
    DartService._krx_listed_codes = listed_codes
    assert DartService.is_listed("005930") is expected


def test_get_corp_rejects_code_absent_from_krx_set(sector_corp):
    DartService._krx_listed_codes = {"005930"}
    with pytest.raises(CompanyNotFoundError, match="상장폐지"):
        DartService.get_corp_by_stock_code("123456")


def test_get_corp_preserves_fallback_when_krx_set_unavailable(sector_corp):
    DartService._krx_listed_codes = None
    assert DartService.get_corp_by_stock_code("123456") is sector_corp


def test_find_corp_logs_listed_and_delisted_fallback(sector_corp, caplog):
    DartService._global_corp_list.find_by_stock_code.side_effect = [None, sector_corp]
    DartService._krx_listed_codes = {"123456"}
    DartService._find_corp("123456")
    assert "KRX 시장정보 부재로 상장폐지 인덱스에서 조회함" in caplog.text

    caplog.clear()
    DartService._global_corp_list.find_by_stock_code.side_effect = [None, sector_corp]
    DartService._krx_listed_codes = set()
    DartService._find_corp("123456")
    assert "상장폐지 종목으로 확인됨" in caplog.text


# ── _apply_corrections (5개) ─────────────────────────────────────────────────

def _make_extracted(year: int, **kwargs) -> dict:
    defaults = dict(
        revenue=0, operating_profit=0, cost_of_sales=0, gross_profit=0, sga=0,
        net_income=0, total_assets=0, total_liabilities=0, equity=0, cash=0,
    )
    defaults.update(kwargs)
    return {year: defaults}


def test_apply_corrections_it_sector_derives_sga():
    """IT 업종(cos=0): sga = revenue - operating_profit."""
    extracted = _make_extracted(2024, revenue=1000, operating_profit=200, cost_of_sales=0)
    result = DartService._apply_corrections(extracted, "테스트", "000001", "보고서", False)
    assert result[0]["sga"] == 800


def test_apply_corrections_manufacturing_derives_sga():
    """제조업(cos>0, gp>0): sga = gross_profit - operating_profit."""
    extracted = _make_extracted(
        2024, revenue=1000, operating_profit=100, cost_of_sales=600, gross_profit=400
    )
    result = DartService._apply_corrections(extracted, "테스트", "000001", "보고서", False)
    assert result[0]["sga"] == 300


def test_apply_corrections_derives_gross_profit_then_sga():
    """gross_profit=0일 때 rev-cos로 파생 후 sga까지 보정한다."""
    extracted = _make_extracted(
        2024, revenue=1000, operating_profit=100, cost_of_sales=600, gross_profit=0
    )
    result = DartService._apply_corrections(extracted, "테스트", "000001", "보고서", False)
    assert result[0]["gross_profit"] == 400
    assert result[0]["sga"] == 300


def test_apply_corrections_skips_sga_when_already_set():
    """sga가 이미 있으면 보정하지 않는다."""
    extracted = _make_extracted(2024, revenue=1000, operating_profit=200, sga=999)
    result = DartService._apply_corrections(extracted, "테스트", "000001", "보고서", False)
    assert result[0]["sga"] == 999


def test_apply_corrections_skips_financial_sector():
    """금융업(is_financial=True)은 보정 로직을 건너뛰고 미수집 필드를 None으로 저장한다."""
    extracted = _make_extracted(2024, revenue=1000, operating_profit=200)
    result = DartService._apply_corrections(extracted, "테스트", "000001", "보고서", True)
    # 금융업은 수집 대상이 아닌 필드가 None으로 반환되어야 함 (0과 미수집 구분)
    assert result[0]["sga"] is None
    assert result[0]["revenue"] is None
    assert result[0]["operating_profit"] is None


def test_apply_corrections_financial_sector_includes_phase90_fields():
    extracted = _make_extracted(
        2024,
        net_interest_income=100,
        loan_loss_provision=-20,
        insurance_liability=300,
    )

    with patch.object(DartService, "classify_financial_sector", return_value="bank"):
        result = DartService._apply_corrections(extracted, "KB", "105560", "report", True)

    assert result[0]["net_interest_income"] == 100
    assert result[0]["loan_loss_provision"] == -20
    assert result[0]["insurance_liability"] == 300
    assert result[0]["sector_detail"] == "bank"


def test_apply_corrections_financial_sector_blank_sector_becomes_none():
    extracted = _make_extracted(2024, net_interest_income=100)

    with patch.object(DartService, "classify_financial_sector", return_value=""):
        result = DartService._apply_corrections(extracted, "KB", "105560", "report", True)

    assert result[0]["sector_detail"] is None


def test_apply_corrections_skips_empty_year():
    """데이터가 없는 연도는 결과에 포함되지 않는다."""
    extracted = {2022: {}, 2023: {"revenue": 100}, 2024: {}}
    result = DartService._apply_corrections(extracted, "테스트", "000001", "보고서", False)
    assert len(result) == 1
    assert result[0]["fiscal_year"] == 2023


def test_apply_corrections_output_has_all_fields():
    """반환된 파라미터 딕셔너리가 FinancialStatement 필드를 모두 포함한다."""
    extracted = _make_extracted(2024, revenue=500, net_income=50, total_assets=1000, equity=700)
    result = DartService._apply_corrections(extracted, "삼성전자", "005930", "보고서", False)
    row = result[0]
    for field in ["company_id", "company_name", "fiscal_year", "revenue", "cost_of_sales",
                  "gross_profit", "sga", "operating_profit", "net_income",
                  "total_assets", "total_liabilities", "equity", "cash", "source_report"]:
        assert field in row, f"'{field}' 필드가 없음"
    assert row["company_id"] == "005930"
    assert row["company_name"] == "삼성전자"


def test_apply_corrections_passes_phase96_fields():
    """비금융 수집 데이터의 Phase 96 필드가 그대로 전달되고, 미수집분은 None이다."""
    extracted = _make_extracted(
        2024, revenue=1000, operating_profit=100,
        finance_income=50, finance_cost=30, income_tax_expense=20, operating_cash_flow=150,
    )
    result = DartService._apply_corrections(extracted, "테스트", "000001", "보고서", False)
    row = result[0]
    assert row["finance_income"] == 50
    assert row["finance_cost"] == 30
    assert row["income_tax_expense"] == 20
    assert row["operating_cash_flow"] == 150
    # 미수집 필드는 None — 0으로 뭉개지 않는다
    assert row["other_income"] is None
    assert row["other_expense"] is None
    assert row["income_before_tax"] is None


def test_apply_corrections_financial_sector_phase96_fields_none():
    """금융업 행은 Phase 96 필드를 수집하지 않는다 — dict에 없거나 None."""
    extracted = _make_extracted(2024, net_income=100, finance_income=999)
    with patch.object(DartService, "classify_financial_sector", return_value="bank"):
        result = DartService._apply_corrections(extracted, "KB", "105560", "report", True)
    # 금융업 dict는 Phase 96 키를 명시하지 않음 — DB nullable 컬럼이 None으로 저장
    assert result[0].get("finance_income") is None
    assert result[0].get("operating_cash_flow") is None


# ── fetch_and_process_data 재시도 로직 (3개) ─────────────────────────────────
#
# fetch_and_process_data는 내부에서 SessionLocal()을 호출하고 마지막에 db.close()를
# 실행하므로, 테스트에서 db_session을 직접 주입하면 닫힌 세션이 된다.
# 대신 test_engine 기반 Session 팩토리를 SessionLocal로 패치하고,
# 검증은 별도로 열어둔 세션으로 수행한다.

def _make_corp_mock(corp_name: str, sector: str = ""):
    corp = MagicMock()
    corp.corp_name = corp_name
    corp.sector = sector
    corp_list = MagicMock()
    corp_list.find_by_stock_code.return_value = corp
    return corp_list, corp


def test_fetch_falls_back_to_delisting_index_when_krx_lookup_misses(caplog):
    corp = MagicMock(corp_name="시프트업", corp_code="01763278", sector="게임 소프트웨어 개발")
    corp_list = MagicMock()
    corp_list.find_by_stock_code.side_effect = [None, corp]
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = MagicMock()

    with patch.object(DartService, "_global_corp_list", corp_list), \
         patch.object(DartService, "_collect_financial_data", side_effect=ValueError("stop")), \
         patch("app.db.database.SessionLocal", return_value=db):
        DartService.fetch_and_process_data("462870")

    assert corp_list.find_by_stock_code.call_args_list == [
        (("462870",),),
        (("462870",), {"include_delisting": True}),
    ]
    assert "KRX 시장정보 부재로 상장폐지 인덱스에서 조회함" in caplog.text


def test_fetch_keeps_existing_not_found_error_when_both_lookups_miss():
    corp_list = MagicMock()
    corp_list.find_by_stock_code.return_value = None
    task = MagicMock()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = task

    with patch.object(DartService, "_global_corp_list", corp_list), \
         patch("app.db.database.SessionLocal", return_value=db):
        DartService.fetch_and_process_data("999999")

    assert corp_list.find_by_stock_code.call_args_list == [
        (("999999",),),
        (("999999",), {"include_delisting": True}),
    ]
    assert "기업 리스트에서 '999999'를 찾을 수 없습니다" in task.message


def test_fetch_retries_on_network_error(test_engine):
    """_collect_financial_data가 네트워크 오류로 실패하면 재시도 후 성공한다."""
    from sqlalchemy.orm import sessionmaker
    from app.services.dart_service import DartService
    from app.db.models import CollectionTask

    Session = sessionmaker(bind=test_engine)
    with Session() as s:
        s.add(CollectionTask(stock_code="005930", status="processing"))
        s.commit()

    corp_list, corp = _make_corp_mock("삼성전자", "반도체")
    extracted = {2024: {"revenue": 1000, "operating_profit": 100}}
    saved_row = {
        "company_id": "005930", "company_name": "삼성전자", "fiscal_year": 2024,
        "revenue": 1000, "cost_of_sales": 0, "gross_profit": 0, "sga": 0,
        "operating_profit": 100, "net_income": 0, "total_assets": 0,
        "total_liabilities": 0, "equity": 0, "cash": 0, "source_report": "보고서",
    }

    with patch.object(DartService, "_global_corp_list", corp_list), \
         patch.object(DartService, "_collect_financial_data",
                      side_effect=[ConnectionError("네트워크 오류"), extracted]), \
         patch.object(DartService, "_apply_corrections", return_value=[saved_row]), \
         patch("app.services.dart_service.time.sleep") as mock_sleep, \
         patch("app.db.database.SessionLocal", Session):
        DartService.fetch_and_process_data("005930")

    mock_sleep.assert_called_once_with(5)
    with Session() as s:
        task = s.query(CollectionTask).filter_by(stock_code="005930").first()
        assert task.status == "completed"


def test_fetch_fails_after_all_retries(test_engine):
    """모든 재시도 횟수를 소진하면 task.status가 failed로 기록된다."""
    from sqlalchemy.orm import sessionmaker
    from app.services.dart_service import DartService
    from app.db.models import CollectionTask

    Session = sessionmaker(bind=test_engine)
    with Session() as s:
        s.add(CollectionTask(stock_code="000001", status="processing"))
        s.commit()

    corp_list, _ = _make_corp_mock("실패기업")

    with patch.object(DartService, "_global_corp_list", corp_list), \
         patch.object(DartService, "_collect_financial_data",
                      side_effect=ConnectionError("계속 실패")), \
         patch("app.services.dart_service.time.sleep"), \
         patch("app.db.database.SessionLocal", Session):
        DartService.fetch_and_process_data("000001")

    with Session() as s:
        task = s.query(CollectionTask).filter_by(stock_code="000001").first()
        assert task.status == "failed"
        assert "계속 실패" in task.message


def test_fetch_does_not_retry_on_value_error(test_engine):
    """ValueError(논리 오류)는 재시도하지 않고 즉시 failed로 기록된다."""
    from sqlalchemy.orm import sessionmaker
    from app.services.dart_service import DartService
    from app.db.models import CollectionTask

    Session = sessionmaker(bind=test_engine)
    with Session() as s:
        s.add(CollectionTask(stock_code="000002", status="processing"))
        s.commit()

    corp_list, _ = _make_corp_mock("테스트기업")

    with patch.object(DartService, "_global_corp_list", corp_list), \
         patch.object(DartService, "_collect_financial_data",
                      side_effect=ValueError("데이터 없음")), \
         patch("app.services.dart_service.time.sleep") as mock_sleep, \
         patch("app.db.database.SessionLocal", Session):
        DartService.fetch_and_process_data("000002")

    mock_sleep.assert_not_called()
    with Session() as s:
        task = s.query(CollectionTask).filter_by(stock_code="000002").first()
        assert task.status == "failed"


def test_classify_financial_sector_maps_bank():
    with patch.object(DartService, "get_sector_by_stock_code", return_value="기타 금융업"):
        assert DartService.classify_financial_sector("105560") == "bank"


def test_classify_financial_sector_maps_insurance():
    with patch.object(DartService, "get_sector_by_stock_code", return_value="보험업"):
        assert DartService.classify_financial_sector("032830") == "insurance"


def test_classify_financial_sector_maps_securities():
    with patch.object(DartService, "get_sector_by_stock_code", return_value="금융 지원 서비스업"):
        assert DartService.classify_financial_sector("005940") == "securities"


def test_classify_financial_sector_degrades_when_sector_missing():
    with patch.object(DartService, "get_sector_by_stock_code", return_value=None):
        assert DartService.classify_financial_sector("105560") == ""


@pytest.mark.parametrize("method,expected", [
    ("get_corp_by_stock_code", "corp"),
    ("get_sector_by_stock_code", "제조업"),
    ("get_company_name_by_stock_code", "테스트기업"),
])
def test_lookup_uses_primary_result_without_delisting(method, expected, dart_service_state):
    corp = MagicMock(corp_name="테스트기업", sector="제조업")
    corp_list = MagicMock()
    corp_list.find_by_stock_code.return_value = corp
    DartService._global_corp_list = corp_list

    result = getattr(DartService, method)("123456")

    assert result == (corp if expected == "corp" else expected)
    corp_list.find_by_stock_code.assert_called_once_with("123456")


@pytest.mark.parametrize("method,expected", [
    ("get_corp_by_stock_code", "corp"),
    ("get_sector_by_stock_code", "제조업"),
    ("get_company_name_by_stock_code", "테스트기업"),
])
def test_lookup_falls_back_even_when_krx_is_healthy(method, expected, dart_service_state, caplog):
    corp = MagicMock(corp_name="테스트기업", sector="제조업")
    corp_list = MagicMock()
    corp_list.find_by_stock_code.side_effect = [None, corp]
    DartService._global_corp_list = corp_list
    assert DartService._krx_degraded is False

    result = getattr(DartService, method)("123456")

    assert result == (corp if expected == "corp" else expected)
    assert corp_list.find_by_stock_code.call_args_list == [
        call("123456"), call("123456", include_delisting=True),
    ]
    assert "[123456] KRX 시장정보 부재로 상장폐지 인덱스에서 조회함" in caplog.text
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_get_corp_keeps_not_found_when_both_lookups_miss(dart_service_state):
    corp_list = MagicMock()
    corp_list.find_by_stock_code.return_value = None
    DartService._global_corp_list = corp_list

    with pytest.raises(CompanyNotFoundError, match="해당 종목을 찾을 수 없습니다"):
        DartService.get_corp_by_stock_code("123456")

    assert corp_list.find_by_stock_code.call_args_list == [
        call("123456"), call("123456", include_delisting=True),
    ]


@pytest.fixture
def sector_corp(dart_service_state):
    from types import SimpleNamespace
    corp = SimpleNamespace(corp_code="00123456", sector=None, _sector=None)
    corp_list = MagicMock()
    corp_list.find_by_stock_code.return_value = corp
    corp_list._corp_codes = {}
    DartService._global_corp_list = corp_list
    return corp


def test_sector_preserves_krx_without_dart(sector_corp):
    sector_corp.sector = "기존 KRX 업종"
    with patch.object(DartService, "_fetch_induty_code") as fetch:
        assert DartService.get_sector_by_stock_code("123456") == "기존 KRX 업종"
    fetch.assert_not_called()


@pytest.mark.parametrize("code,label", [
    ("64992", "금융업"),
    ("26410", "전자부품·컴퓨터·영상·음향 및 통신장비 제조업"),
])
def test_sector_uses_ksic_label_when_krx_missing(sector_corp, code, label):
    with patch.object(DartService, "_fetch_induty_code", return_value=code) as fetch:
        assert DartService.get_sector_by_stock_code("123456") == label
    fetch.assert_called_once_with(sector_corp.corp_code)


def test_sector_unknown_division_keeps_code(sector_corp):
    with patch.object(DartService, "_fetch_induty_code", return_value="99999"):
        assert DartService.get_sector_by_stock_code("123456") == "KSIC 99"


def test_all_measured_missing_ksic_divisions_are_labeled():
    missing_codes = ("10", "19", "22", "24", "28", "31", "35", "50", "51", "52", "60", "61", "96")

    for code in missing_codes:
        label = DartService.KSIC_DIVISION_LABELS.get(code)
        assert label is not None
        assert not label.startswith("KSIC ")


def test_ksic_division_labels_are_unique():
    labels = DartService.KSIC_DIVISION_LABELS.values()
    assert len(set(labels)) == len(DartService.KSIC_DIVISION_LABELS)


@pytest.mark.parametrize("code,expected", [
    ("65100", "insurance"), ("66100", "securities"), ("64992", "bank"),
])
def test_classify_uses_ksic_before_insurance_substring(sector_corp, code, expected):
    with patch.object(DartService, "_fetch_induty_code", return_value=code):
        assert DartService.classify_financial_sector("123456") == expected


def test_sector_returns_none_when_dart_fails(sector_corp):
    with patch("app.services.dart_service.requests.get", side_effect=OSError("offline")):
        assert DartService.get_sector_by_stock_code("123456") is None
    assert DartService._induty_code_cache == {}


def test_sector_peer_divisions_never_share_unknown_label(sector_corp):
    from types import SimpleNamespace
    DartService._global_corp_list.find_by_stock_code.side_effect = [
        sector_corp, SimpleNamespace(corp_code="00876543", sector=None, _sector=None),
    ]
    with patch.object(DartService, "_fetch_induty_code", side_effect=["98999", "99999"]):
        first = DartService.get_sector_by_stock_code("123456")
        second = DartService.get_sector_by_stock_code("654321")
    assert first is not None and second is not None
    assert first != second


@pytest.mark.parametrize("sector,expected", [
    ("보험업", "insurance"), ("금융 지원 서비스업", "securities"), ("기타 금융업", "bank"),
])
def test_classify_preserves_krx_when_ksic_unavailable(sector_corp, sector, expected):
    sector_corp.sector = sector
    with patch.object(DartService, "_fetch_induty_code", return_value=None):
        assert DartService.classify_financial_sector("123456") == expected
