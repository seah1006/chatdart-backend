import re
import time
import threading
import requests
import dart_fss as dart
from datetime import datetime, timedelta
from app.db.models import AIAnalysisCache, FinancialStatement, CollectionTask
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

class ServiceNotReadyError(Exception):
    """데이터 로딩 등 서비스가 아직 준비되지 않았을 때 발생"""
    pass

class CompanyNotFoundError(Exception):
    """해당하는 기업을 찾을 수 없을 때 발생"""
    pass


def _coalesce_net_income_basis(extracted: dict, years, label: str = "") -> None:
    """net_income을 지배주주(net_income_owners) 우선, 없으면 연결총(net_income_total)으로 확정한다 (Phase 106).

    fast-path·slow-path가 두 concept를 각각 net_income_owners/net_income_total로 채운 뒤 호출된다.
    중간 키는 제거하고, 둘 다 없으면 net_income 키를 만들지 않아 기존 결측 처리를 유지한다.

    Phase 107(AI 최종 비준 2026-07-23): fallback이 조용히 기준 혼합으로 이어지지 않도록
    종목·연도별 net_income 출처 기준(owners_of_parent / consolidated_total_fallback)을 로그로 남긴다.
    """
    for y in years:
        d = extracted[y]
        owners = d.pop("net_income_owners", None)
        total = d.pop("net_income_total", None)
        if owners is not None:
            d["net_income"] = owners
            basis = "owners_of_parent"
        elif total is not None:
            d["net_income"] = total
            basis = "consolidated_total_fallback"
        else:
            continue
        logger.info("net_income 기준 [%s] %s: %s (basis=%s)", label or "?", y, d["net_income"], basis)


class DartService:
    CONCEPT_MAPPING = {
        'ifrs-full_Revenue': 'revenue',
        'ifrs-full_SalesRevenueNet': 'revenue',
        'ifrs-full_CostOfSales': 'cost_of_sales',
        'ifrs-full_GrossProfit': 'gross_profit',
        'ifrs-full_SellingGeneralAndAdministrativeExpenses': 'sga',
        'dart_SellingGeneralAdministrativeExpenses': 'sga',
        'ifrs-full_OperatingExpenses': 'sga',
        'dart_OperatingExpenses': 'sga',
        'dart_OperatingIncomeLoss': 'operating_profit',
        'k-ifrs_OperatingIncomeLoss': 'operating_profit',
        'ifrs-full_ProfitLoss': 'net_income_total',                              # 연결 총 순이익(지배+비지배)
        'ifrs-full_ProfitLossAttributableToOwnersOfParent': 'net_income_owners',  # 지배주주지분
        'ifrs-full_Assets': 'total_assets',
        'ifrs-full_Liabilities': 'total_liabilities',
        'ifrs-full_Equity': 'equity',
        'ifrs-full_CashAndCashEquivalents': 'cash',
        # 손익 세부 항목 (Phase 96)
        'ifrs-full_OtherIncome': 'other_income',
        'dart_OtherGains': 'other_income',
        'dart_NonOperatingIncome': 'other_income',      # 기아 등 일부 기업의 기타수익
        'ifrs-full_OtherExpenseByFunction': 'other_expense',
        'dart_OtherLosses': 'other_expense',
        'dart_NonOperatingExpense': 'other_expense',
        'ifrs-full_FinanceIncome': 'finance_income',
        'ifrs-full_FinanceCosts': 'finance_cost',
        'ifrs-full_ProfitLossBeforeTax': 'income_before_tax',
        'ifrs-full_IncomeTaxExpenseContinuingOperations': 'income_tax_expense',
        'ifrs-full_CashFlowsFromUsedInOperatingActivities': 'operating_cash_flow',
    }

    # 금융업(은행·보험·증권)은 DART 공시 구조가 달라 공통 지표만 매핑
    FINANCIAL_CONCEPT_MAPPING = {
        'ifrs-full_Assets': 'total_assets',
        'ifrs-full_Liabilities': 'total_liabilities',
        'ifrs-full_Equity': 'equity',
        'ifrs-full_ProfitLoss': 'net_income_total',                              # 연결 총 순이익(지배+비지배)
        'ifrs-full_ProfitLossAttributableToOwnersOfParent': 'net_income_owners',  # 지배주주지분
        'ifrs-full_CashAndCashEquivalents': 'cash',
        # 순이자손익(은행) — 손익계산서 순이자이익. 현금흐름표 동명 계정은 별도 concept_id라 여기서 안 잡힘.
        'ifrs-full_InterestRevenueExpense': 'net_interest_income',
        # 신용손실충당금 전입/환입액(은행) — 보고서 연도마다 라벨·concept_id가 바뀜(IFRS9 손상차손).
        'ifrs-full_ImpairmentLossImpairmentGainAndReversalOfImpairmentLossDeterminedInAccordanceWithIFRS9': 'loan_loss_provision',
    }

    FINANCIAL_ACCOUNT_NAME_MAPPING = {
        '순이자이익': 'net_interest_income',
        '이자순수익': 'net_interest_income',
        '순이자수익': 'net_interest_income',
        '순이자손익': 'net_interest_income',
        '대손충당금전입액': 'loan_loss_provision',
        '신용손실충당금전입액': 'loan_loss_provision',
        '신용손실충당금(전)환입액': 'loan_loss_provision',
        '보험계약부채': 'insurance_liability',
    }

    # 비금융 손익 세부 항목 — concept_id 표류 대비 한글 계정명 폴백 (Phase 96)
    # 주의: _fetch_open_api_year가 account_nm의 공백을 제거한 뒤 매칭하므로 키에 공백 금지
    GENERAL_ACCOUNT_NAME_MAPPING = {
        # 지배주주지분 순이익 라벨 폴백 (Phase 106) — concept_id 미검출 기업 보강용.
        # '순이익' 접미사 키만 사용: 바 형태('지배기업소유주지분')는 재무상태표 자본 항목
        # (EquityAttributableToOwnersOfParent)과 라벨이 겹쳐 abs-max가 자본총계를 잘못 채택하므로 제외.
        '지배기업소유주지분순이익': 'net_income_owners',
        '지배주주지분순이익': 'net_income_owners',
        '기타수익': 'other_income',
        '기타영업외수익': 'other_income',
        '기타이익': 'other_income',
        '기타비용': 'other_expense',
        '기타영업외비용': 'other_expense',
        '기타손실': 'other_expense',
        '금융수익': 'finance_income',
        '금융비용': 'finance_cost',
        '금융원가': 'finance_cost',
        '법인세비용차감전순이익': 'income_before_tax',
        '법인세비용차감전이익': 'income_before_tax',
        '법인세차감전순이익': 'income_before_tax',
        '법인세비용차감전계속영업이익': 'income_before_tax',
        '법인세비용': 'income_tax_expense',
        '영업활동현금흐름': 'operating_cash_flow',
        '영업활동으로인한현금흐름': 'operating_cash_flow',
        '영업활동순현금흐름': 'operating_cash_flow',
    }

    # DART sector 값 기준: '기타 금융업'(은행·금융지주), '보험업', '금융 지원 서비스업'(증권)
    FINANCIAL_SECTOR_KEYWORDS = ['금융', '보험']

    # DART 공시 단위가 기업마다 다름 — DB는 항상 원(KRW) 단위로 통일
    UNIT_MULTIPLIER = {
        '원':     1,
        '천원':   1_000,
        '백만원': 1_000_000,
        '억원':   100_000_000,
    }

    _global_corp_list = None
    _init_failed = False          # 초기화 실패 여부 — 헬스 체크 및 에러 메시지에 활용
    _krx_degraded = False         # KRX 시장정보 부재 여부 — DART 폴백은 계속 사용 가능
    _krx_checked = False          # initialize 가 KRX 판정을 끝냈는지 — /health 의 loading/unknown 구분
    _krx_listed_codes: set[str] | None = None
    _induty_code_cache: dict[str, str | None] = {}
    _stock_corps = []             # 상장 기업만 필터링한 리스트 (stock_code 있는 것)
    _name_index: dict = {}        # corp_name → stock_code O(1) 정확 매칭용
    _reinit_lock = threading.Lock()  # 관리자 수동 재초기화 동시 실행 방지

    # DART 표준명과 다른 사용자 입력을 종목코드로 매핑
    # 키: 소문자 정규화된 alias, 값: 종목코드(6자리)
    COMPANY_ALIASES: dict[str, str] = {
        # SK하이닉스
        "sk하이닉스": "000660",
        "skhynix": "000660",
        "hynix": "000660",
        "하이닉스": "000660",
        # SK스퀘어
        "sk스퀘어": "402340",
        "sksquare": "402340",
        # LG에너지솔루션
        "lg에너지솔루션": "373220",
        "lgenergysolution": "373220",
        "lg엔솔": "373220",
        "엘지에너지솔루션": "373220",
        "lg에너지": "373220",
        "lges": "373220",
        # HD현대중공업
        "hd현대중공업": "329180",
        "hdhyundaiheavyindustries": "329180",
        "현대중공업": "329180",
        # KB금융
        "kb금융": "105560",
        "kbfinancial": "105560",
        "국민금융": "105560",
        # 삼성SDI
        "삼성sdi": "006400",
        "samsungsdi": "006400",
        "sdi": "006400",
        # HD현대일렉트릭
        "hd현대일렉트릭": "267260",
        "hdhyundaielectric": "267260",
        "현대일렉트릭": "267260",
        # LS ELECTRIC
        "lselectric": "010120",
        "ls일렉트릭": "010120",
        # LG전자
        "lg전자": "066570",
        "lgelectronics": "066570",
        "엘지전자": "066570",
        # POSCO홀딩스
        "posco홀딩스": "005490",
        "poscoholdings": "005490",
        "포스코홀딩스": "005490",
        # HD한국조선해양
        "hd한국조선해양": "009540",
        "hdkoreashipbuilding": "009540",
        "한국조선해양": "009540",
        # SK텔레콤
        "sk텔레콤": "017670",
        "sktelecom": "017670",
        "skt": "017670",
        # HD현대
        "hd현대": "267250",
        "hdhyundai": "267250",
        # SK이노베이션
        "sk이노베이션": "096770",
        "skinnovation": "096770",
        # LIG디펜스앤에어로스페이스
        "lig디펜스앤에어로스페이스": "079550",
        "lignex1": "079550",
        "lig넥스원": "079550",
        # LG이노텍
        "lg이노텍": "011070",
        "lginnotek": "011070",
        # NH투자증권
        "nh투자증권": "005940",
        "nhinvestment": "005940",
        "nh투자": "005940",
        # HD현대마린솔루션
        "hd현대마린솔루션": "443060",
        "hdhyundaimarinesolution": "443060",
        # DB손해보험
        "db손해보험": "005830",
        "dbinsurance": "005830",
        # 삼성E&A
        "삼성ea": "028050",
        "samsungea": "028050",
        "삼성엔지니어링": "028050",
        # DB하이텍
        "db하이텍": "000990",
        "dbhitek": "000990",
        # HD건설기계
        "hd건설기계": "267270",
        "hd현대건설기계": "267270",
        "hdconstructionequipment": "267270",
        "현대건설기계": "267270",
        # SK바이오팜
        "sk바이오팜": "326030",
        "skbiopharm": "326030",
        # LG디스플레이
        "lg디스플레이": "034220",
        "lgdisplay": "034220",
        # LG유플러스
        "lg유플러스": "032640",
        "lguplus": "032640",
        "유플러스": "032640",
        # BNK금융지주
        "bnk금융지주": "138930",
        "bnkfinancial": "138930",
        # OCI홀딩스
        "oci홀딩스": "010060",
        "ociholdings": "010060",
        # LG생활건강
        "lg생활건강": "051900",
        "lghousehold": "051900",
        "lghh": "051900",
        "엘지생활건강": "051900",
        # SK바이오사이언스
        "sk바이오사이언스": "302440",
        "skbioscience": "302440",
        # CJ제일제당
        "cj제일제당": "097950",
        "cjcheiljedang": "097950",
        # DL이앤씨
        "dl이앤씨": "375500",
        "dlec": "375500",
        # iM금융지주
        "im금융지주": "139130",
        "imfinancial": "139130",
        "dgb금융지주": "139130",
        # HL만도
        "hl만도": "204320",
        "hlmando": "204320",
        "만도": "204320",
        # GS건설
        "gs건설": "006360",
        "gsconstruction": "006360",
        # HD현대마린엔진
        "hd현대마린엔진": "071970",
        "hdhyundaimarineengine": "071970",
        # LS에코에너지
        "ls에코에너지": "229640",
        "lsecoenergy": "229640",
        # DN오토모티브
        "dn오토모티브": "007340",
        "dnautomotive": "007340",
        # 짧은 영문 alias: 지주사 또는 모기업 기준
        "sk": "034730",
        "ls": "006260",
        "gs": "078930",
        "cj": "001040",
        "kt": "030200",
        "skc": "011790",
        # 네이버
        "네이버": "035420",
        "naver": "035420",
        # HMM
        "hmm": "011200",
        # KT&G
        "ktg": "033780",
        "ktng": "033780",
        "케이티앤지": "033780",
        # S-Oil
        "soil": "010950",
        "에쓰오일": "010950",
        # 엔씨소프트
        "엔씨": "036570",
        "nc": "036570",
        "엔씨소프트": "036570",
        "nc소프트": "036570",
        "ncsoft": "036570",
        # KCC
        "kcc": "002380",
        # F&F
        "ff": "383220",
        "fnf": "383220",
        # 카카오
        "카카오": "035720",
        "kakao": "035720",
        # 크래프톤
        "배그": "259960",
        "pubg": "259960",
        "크래프톤": "259960",
        # 넥슨 (NXC는 비상장, 넥슨코리아 상장사 기준)
        "넥슨": "225570",
        "nexon": "225570",
        # 하이브
        "빅히트": "352820",
        "hybe": "352820",
        "하이브": "352820",
        # 삼성전자
        "삼성": "005930",
        "samsung": "005930",
        "삼성전자": "005930",
        # 현대자동차
        "현대차": "005380",
        "hyundai": "005380",
        # 기아
        "기아차": "000270",
        "kia": "000270",
        # 셀트리온
        "셀트리온": "068270",
        "celltrion": "068270",
    }

    # 출처: 통계청 한국표준산업분류(KSIC) 제11차 개정 대분류(division) 기준.
    KSIC_DIVISION_LABELS = {
        "01": "농업",
        "02": "임업",
        "03": "어업",
        "05": "석탄 및 갈탄 광업",
        "06": "원유 및 천연가스 채굴업",
        "07": "금속 광업",
        "08": "비금속 광물 광업; 연료용 제외",
        "09": "광업 지원 서비스업",
        "10": "식료품 제조업",
        "11": "음료 제조업",
        "12": "담배 제조업",
        "13": "섬유제품 제조업; 의복 제외",
        "14": "의복, 의복 액세서리 및 모피제품 제조업",
        "15": "가죽, 가방 및 신발 제조업",
        "16": "목재 및 나무제품 제조업; 가구 제외",
        "17": "펄프, 종이 및 종이제품 제조업",
        "18": "인쇄 및 기록매체 복제업",
        "19": "코크스, 연탄 및 석유정제품 제조업",
        "20": "화학물질 및 화학제품 제조업",
        "21": "의료용 물질 및 의약품 제조업",
        "22": "고무 및 플라스틱제품 제조업",
        "23": "비금속 광물제품 제조업",
        "24": "1차 금속 제조업",
        "25": "금속가공제품 제조업; 기계 및 가구 제외",
        "26": "전자부품·컴퓨터·영상·음향 및 통신장비 제조업",
        "27": "의료, 정밀, 광학기기 및 시계 제조업",
        "28": "전기장비 제조업",
        "29": "기타 기계 및 장비 제조업",
        "30": "자동차 및 트레일러 제조업",
        "31": "기타 운송장비 제조업",
        "32": "가구 제조업",
        "33": "기타 제품 제조업",
        "34": "산업용 기계 및 장비 수리업",
        "35": "전기, 가스, 증기 및 공기 조절 공급업",
        "36": "수도업",
        "37": "하수, 폐수 및 분뇨 처리업",
        "38": "폐기물 수집, 운반, 처리 및 원료 재생업",
        "39": "환경 정화 및 복원업",
        "41": "종합 건설업",
        "42": "전문직별 공사업",
        "45": "자동차 및 부품 판매업",
        "46": "도매 및 상품중개업",
        "47": "소매업",
        "49": "육상 운송 및 파이프라인 운송업",
        "50": "수상 운송업",
        "51": "항공 운송업",
        "52": "창고 및 운송관련 서비스업",
        "55": "숙박업",
        "56": "음식점 및 주점업",
        "58": "출판업",
        "59": "영상·오디오 기록물 제작 및 배급업",
        "60": "방송업",
        "61": "우편 및 통신업",
        "62": "컴퓨터 프로그래밍·시스템 통합 및 관리업",
        "63": "정보서비스업",
        "64": "금융업",
        "65": "보험 및 연금업",
        "66": "금융 및 보험 관련 서비스업",
        "68": "부동산업",
        "70": "연구개발업",
        "71": "전문 서비스업",
        "72": "건축기술, 엔지니어링 및 기타 과학기술 서비스업",
        "73": "기타 전문, 과학 및 기술 서비스업",
        "74": "사업시설 관리 및 조경 서비스업",
        "75": "사업 지원 서비스업",
        "76": "임대업; 부동산 제외",
        "84": "공공 행정, 국방 및 사회보장 행정",
        "85": "교육 서비스업",
        "86": "보건업",
        "87": "사회복지 서비스업",
        "90": "창작, 예술 및 여가관련 서비스업",
        "91": "스포츠 및 오락관련 서비스업",
        "94": "협회 및 단체",
        "95": "개인 및 소비용품 수리업",
        "96": "기타 개인 서비스업",
    }

    # ── 유틸리티 메서드 ──────────────────────────────────────────────────────

    @classmethod
    def _normalize_alias_key(cls, s: str) -> str:
        """alias 검색용 키 정규화: 소문자화 후 공백, &, -, . 제거."""
        return re.sub(r"[\s&\-.]", "", s.lower())

    @classmethod
    def _normalize_to_krw(cls, value: int, unit: str) -> int:
        """금액을 원(KRW) 단위로 정규화. dart_fss 컬럼명에 단위가 포함됨."""
        multiplier = cls.UNIT_MULTIPLIER.get(unit, 1)
        if multiplier != 1:
            logger.debug(f"단위 변환: {value} {unit} → {value * multiplier} 원")
        return value * multiplier

    @classmethod
    def _extract_unit_from_column(cls, col_name: str) -> str:
        """컬럼명에서 단위 추출. 예: '2023(단위:천원)' → '천원', 명시 없으면 '원'."""
        match = re.search(r'단위:(\S+?)[)\s]', str(col_name))
        if match:
            unit = match.group(1).strip()
            if unit in cls.UNIT_MULTIPLIER:
                return unit
            logger.warning(f"알 수 없는 단위 '{unit}' — 기본값 '원' 적용 (컬럼: {col_name})")
        return '원'

    @classmethod
    def _get_corp_sector(cls, corp) -> str | None:
        """corp 객체에서 섹터(업종) 문자열을 추출. 조회 실패 시 None."""
        try:
            sector = getattr(corp, 'sector', None) or getattr(corp, '_sector', None)
            if not sector:
                idx = cls._global_corp_list._corp_codes.get(corp.corp_code)
                if idx is not None:
                    sector = cls._global_corp_list._corp_sector[idx]
            return sector or None
        except Exception:
            return None

    @classmethod
    def _fetch_induty_code(cls, corp_code: str) -> str | None:
        """DART company.json에서 KSIC 업종 코드를 조회해 corp_code별로 캐시."""
        if corp_code in cls._induty_code_cache:
            return cls._induty_code_cache[corp_code]

        induty_code = None
        try:
            response = requests.get(
                "https://opendart.fss.or.kr/api/company.json",
                params={"crtfc_key": settings.DART_API_KEY, "corp_code": corp_code},
                timeout=10,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") == "000" and payload.get("induty_code"):
                induty_code = str(payload["induty_code"])
            else:
                logger.warning(
                    f"DART 업종 코드 조회 실패 [{corp_code}]: "
                    f"status={payload.get('status')}, message={payload.get('message')}"
                )
        except Exception as e:
            logger.warning(f"DART 업종 코드 조회 실패 [{corp_code}]: {e}")

        if induty_code is not None:
            cls._induty_code_cache[corp_code] = induty_code
        return induty_code

    @classmethod
    def _fetch_krx_listed_codes(cls) -> set[str] | None:
        """KRX 공식 API에서 3개 시장의 상장 종목코드 집합을 가져온다."""
        api_key = settings.KRX_API_KEY
        if not api_key:
            return None

        paths = ("stk_isu_base_info", "ksq_isu_base_info", "knx_isu_base_info")
        today = datetime.now().date()
        for days_ago in range(1, 8):
            bas_dd = (today - timedelta(days=days_ago)).strftime("%Y%m%d")
            listed_codes: set[str] = set()
            try:
                for path in paths:
                    response = requests.get(
                        f"https://data-dbg.krx.co.kr/svc/apis/sto/{path}",
                        params={"basDd": bas_dd},
                        headers={"AUTH_KEY": api_key},
                        timeout=10,
                    )
                    response.raise_for_status()
                    records = response.json().get("OutBlock_1") or []
                    if not records:
                        raise LookupError(f"{path} 응답이 비어 있음")
                    listed_codes.update(
                        str(record["ISU_SRT_CD"])
                        for record in records
                        if record.get("ISU_SRT_CD")
                    )
            except LookupError:
                continue
            except Exception as e:
                logger.warning("KRX 상장 종목 조회 실패 [%s]: %s", bas_dd, e)
                return None

            if listed_codes:
                cls._krx_listed_codes = listed_codes
                return listed_codes

        logger.warning("KRX 상장 종목 조회 실패: 최근 7일 내 유효한 응답 없음")
        return None

    @classmethod
    def is_listed(cls, stock_code: str) -> bool | None:
        """KRX 공식 집합 기준 상장 여부를 반환한다. 집합이 없으면 판정 불가다."""
        if cls._krx_listed_codes is None:
            return None
        return stock_code in cls._krx_listed_codes

    @classmethod
    def _find_corp(cls, stock_code: str):
        corp = cls._global_corp_list.find_by_stock_code(stock_code)
        if corp is None:
            corp = cls._global_corp_list.find_by_stock_code(
                stock_code, include_delisting=True
            )
            if corp is not None:
                if cls.is_listed(stock_code) is False:
                    logger.warning(f"[{stock_code}] 상장폐지 종목으로 확인됨")
                else:
                    logger.warning(
                        f"[{stock_code}] KRX 시장정보 부재로 상장폐지 인덱스에서 조회함"
                    )
        return corp

    @classmethod
    def get_sector_by_stock_code(cls, stock_code: str) -> str | None:
        """종목코드로 섹터(업종)를 조회. DART 리스트 미초기화 시 None."""
        if not cls._global_corp_list:
            return None
        try:
            corp = cls._find_corp(stock_code)
            if corp is None:
                return None
            sector = cls._get_corp_sector(corp)
            if sector:
                return sector
            induty_code = cls._fetch_induty_code(corp.corp_code)
            if induty_code:
                code2 = induty_code[:2]
                return cls.KSIC_DIVISION_LABELS.get(code2, f"KSIC {code2}")
            return None
        except Exception:
            return None

    @classmethod
    def get_company_name_by_stock_code(cls, stock_code: str) -> str | None:
        """종목코드로 기업명을 조회. DART 리스트 미초기화 시 None."""
        if not cls._global_corp_list:
            return None
        try:
            corp = cls._find_corp(stock_code)
            return corp.corp_name if corp else None
        except Exception:
            return None

    @classmethod
    def get_corp_by_stock_code(cls, stock_code: str):
        """종목코드로 corp 객체(corp_code 보유) 조회.

        리스트 미로딩 시 ServiceNotReadyError, 없는 종목은 CompanyNotFoundError.
        """
        if not cls._global_corp_list:
            if cls._init_failed:
                raise ServiceNotReadyError("DART 기업 리스트 초기화에 실패했습니다. 서버 관리자에게 문의하세요.")
            raise ServiceNotReadyError("기업 리스트 로딩 중입니다. 잠시 후 다시 시도해주세요.")
        corp = cls._find_corp(stock_code)
        if corp is None:
            raise CompanyNotFoundError("해당 종목을 찾을 수 없습니다.")
        if cls.is_listed(stock_code) is False:
            raise CompanyNotFoundError("상장폐지 종목으로 확인되어 조회할 수 없습니다.")
        return corp

    @classmethod
    def classify_financial_sector(cls, stock_code: str) -> str:
        """Return AI financial sub-sector: bank, insurance, securities, or empty."""
        try:
            corp = cls._find_corp(stock_code) if cls._global_corp_list else None
            induty_code = cls._fetch_induty_code(corp.corp_code) if corp is not None else None
            if induty_code:
                return {"65": "insurance", "66": "securities", "64": "bank"}.get(induty_code[:2], "")
        except Exception as e:
            logger.warning("금융 세분류 KSIC 조회 실패 [%s]: %s", stock_code, e)
        sector = cls.get_sector_by_stock_code(stock_code)
        if not sector:
            return ""
        if "보험" in sector:
            return "insurance"
        if "금융 지원" in sector or "증권" in sector:
            return "securities"
        if "금융" in sector:
            return "bank"
        return ""

    @classmethod
    def _is_financial_sector(cls, corp) -> bool:
        """금융업 여부 확인. 금융업은 DART 공시 구조가 달라 별도 매핑이 필요."""
        try:
            sector = cls._get_corp_sector(corp)
            if sector:
                return any(keyword in sector for keyword in cls.FINANCIAL_SECTOR_KEYWORDS)
            induty_code = cls._fetch_induty_code(corp.corp_code)
            return bool(induty_code and induty_code[:2] in {"64", "65", "66"})
        except Exception as e:
            logger.warning(f"섹터 확인 실패 [{corp.corp_name}]: {e} — 일반 기업으로 처리")
            return False

    # ── 기업 조회 메서드 ─────────────────────────────────────────────────────

    @classmethod
    def _get_corp_list_with_retry(cls, _retry_delays):
        last_error = None
        for attempt in range(len(_retry_delays) + 1):
            try:
                return dart.get_corp_list()
            except Exception as e:
                last_error = e
                if attempt >= len(_retry_delays):
                    raise
                delay = _retry_delays[attempt]
                logger.warning(
                    "DART 기업 리스트 초기화 재시도 "
                    f"{attempt + 1}/{len(_retry_delays)}: {e}. {delay}초 후 재시도합니다."
                )
                time.sleep(delay)
        raise last_error

    @classmethod
    def initialize(cls, _retry_delays=(5, 15)):
        """서버 기동 시 한 번만 실행되어 기업 리스트를 메모리에 캐싱.

        백그라운드 스레드에서 호출되므로 예외가 발생해도 서버 프로세스가
        종료되지 않도록 최상위에서 반드시 catch 처리.
        """
        try:
            logger.info("DART 기업 리스트 동기화 시작 (116,000+ 기업, 약 1~2분 소요)")
            dart.set_api_key(api_key=settings.DART_API_KEY)
            corp_list = cls._get_corp_list_with_retry(_retry_delays)
            # _stock_corps/_name_index를 먼저 구축한 뒤 _global_corp_list를 할당 —
            # 순서를 바꾸면 _global_corp_list는 설정됐지만 인덱스가 비어있는 레이스 컨디션이 생긴다.
            stock_corps = [c for c in corp_list.corps if c.stock_code]
            name_index = {c.corp_name: c.stock_code for c in stock_corps}
            cls._stock_corps = stock_corps
            cls._name_index = name_index
            cls._global_corp_list = corp_list
            cls._init_failed = False
            cls._krx_listed_codes = None
            cls._krx_checked = False
            if cls._fetch_krx_listed_codes() is not None:
                cls._krx_degraded = False
            else:
                try:
                    cls._krx_degraded = corp_list.find_by_stock_code("005930") is None
                except Exception as e:
                    cls._krx_degraded = True
                    logger.error(f"KRX 시장정보 프로브 실패: {e}")
            cls._krx_checked = True
            if cls._krx_degraded:
                logger.error(
                    "KRX 시장정보가 비어 있습니다: 삼성전자(005930)를 상장 인덱스에서 "
                    "찾지 못했습니다. DART 폴백으로 서비스를 계속합니다."
                )
            logger.info(
                "DART 기업 리스트 동기화 완료 "
                f"(총 {len(corp_list.corps)}개 기업, "
                f"상장사 {len(stock_corps)}개 인덱싱 완료)"
            )
        except Exception as e:
            cls._init_failed = True
            logger.exception(
                "DART 기업 리스트 초기화 실패. /health 엔드포인트가 degraded를 반환합니다. "
                f"원인: {e}"
            )

    @classmethod
    def trigger_reinitialize(cls) -> str:
        """관리자 수동 DART 재초기화. 초기화가 영구 실패한 상태에서만 백그라운드 재시도.

        서버 재시작 없이 degraded 상태를 복구하기 위한 용도다.

        반환값:
          - "already_ready":   이미 로딩 완료 — 재초기화 불필요
          - "loading":         최초 로딩 진행 중 — 재초기화 불필요
          - "reinitializing":  재초기화 백그라운드 스레드를 시작함
          - "already_running": 재초기화가 이미 진행 중
        """
        if cls._global_corp_list is not None:
            return "already_ready"
        if not cls._init_failed:
            return "loading"
        if not cls._reinit_lock.acquire(blocking=False):
            return "already_running"

        # 재시도 동안에는 resolve/search가 '로딩 중'으로 응답하도록 실패 플래그를 내린다.
        cls._init_failed = False

        def _run() -> None:
            try:
                cls.initialize()
            finally:
                cls._reinit_lock.release()

        threading.Thread(target=_run, daemon=True, name="dart-reinit").start()
        return "reinitializing"

    @classmethod
    def resolve_stock_code(cls, identifier: str) -> str:
        """기업명 또는 코드를 종목 코드로 변환"""
        if not cls._global_corp_list:
            if cls._init_failed:
                raise ServiceNotReadyError("DART 기업 리스트 초기화에 실패했습니다. 서버 관리자에게 문의하세요.")
            raise ServiceNotReadyError("기업 리스트 로딩 중입니다. 잠시 후 다시 시도해주세요.")

        identifier = identifier.strip()
        if identifier.isdigit() and len(identifier) == 6:
            return identifier

        # alias 우선 체크
        alias_key = cls._normalize_alias_key(identifier)
        if alias_key in cls.COMPANY_ALIASES:
            return cls.COMPANY_ALIASES[alias_key]

        # O(1) 정확 매칭
        if identifier in cls._name_index:
            return cls._name_index[identifier]

        # O(상장사 수) 매칭 — 전방일치 우선, 없으면 부분 매칭
        partial_match = next(
            (c for c in cls._stock_corps if c.corp_name.startswith(identifier)), None
        ) or next(
            (c for c in cls._stock_corps if identifier in c.corp_name), None
        )
        if partial_match:
            return partial_match.stock_code

        raise CompanyNotFoundError(f"'{identifier}'에 해당하는 상장 기업을 찾을 수 없습니다.")

    @classmethod
    def search_companies(cls, query: str):
        """자동완성용 검색 — alias 매칭 > 정확일치 > 전방일치 > 포함 순으로 정렬"""
        if not cls._global_corp_list:
            if cls._init_failed:
                raise ServiceNotReadyError("DART 기업 리스트 초기화에 실패했습니다. 서버 관리자에게 문의하세요.")
            raise ServiceNotReadyError("기업 리스트 로딩 중입니다. 잠시 후 다시 시도해주세요.")

        alias_key = cls._normalize_alias_key(query)
        alias_code = cls.COMPANY_ALIASES.get(alias_key)

        results = [c for c in cls._stock_corps if query in c.corp_name]
        results.sort(key=lambda c: (
            0 if c.corp_name == query else
            1 if c.corp_name.startswith(query) else
            2
        ))

        if alias_code:
            alias_index = next(
                (i for i, c in enumerate(results) if c.stock_code == alias_code), None
            )
            if alias_index is not None:
                results.insert(0, results.pop(alias_index))
            else:
                alias_corp = next(
                    (c for c in cls._stock_corps if c.stock_code == alias_code), None
                )
                if alias_corp:
                    results.insert(0, alias_corp)

        return results[:10]

    # ── 데이터 수집 메서드 ───────────────────────────────────────────────────

    @classmethod
    def _parse_fs_into(
        cls,
        fs,
        years: list,
        concept_mapping: dict,
        extracted: dict,
        name_mapping: dict | None = None,
    ) -> None:
        """extract_fs 결과를 파싱해 extracted dict에 병합. 재사용 가능한 내부 헬퍼."""
        for name in ['is', 'cis', 'bs', 'cbs']:
            try:
                df = fs[name]
                if df is None or df.empty:
                    continue
                cols = df.columns
                concept_col = next((c for c in cols if 'concept_id' in str(c)), None)
                label_cols = [
                    c for c in cols
                    if any(token in str(c).lower() for token in ('label_ko', 'account_nm', 'account_name', 'label'))
                ]
                for year in years:
                    val_cols = [c for c in cols if str(year) in str(c)]
                    if not val_cols:
                        continue
                    value_col = val_cols[0]
                    unit = cls._extract_unit_from_column(value_col)
                    for _, row in df.iterrows():
                        c_id = row[concept_col] if concept_col is not None else None
                        key = concept_mapping.get(c_id)
                        if not key and name_mapping:
                            for label_col in label_cols:
                                acc_nm = re.sub(r'\s+', '', str(row.get(label_col, '')))
                                key = name_mapping.get(acc_nm)
                                if key:
                                    break
                        if not key:
                            continue
                        val = str(row[value_col]).strip()
                        if val and val != 'nan':
                            if val.startswith('('):
                                val = '-' + val[1:-1]
                            raw_val = int(float(val.replace(',', '')))
                            clean_val = cls._normalize_to_krw(raw_val, unit)
                            if key not in extracted[year] or abs(clean_val) > abs(extracted[year].get(key, 0)):
                                extracted[year][key] = clean_val
            except Exception as e:
                logger.warning(f"재무제표 '{name}' 파싱 실패: {e}")

    @classmethod
    def _parse_open_api_amount(cls, raw) -> int | None:
        """fnlttSinglAcntAll 금액 문자열을 원(KRW) 정수로 변환. 빈 값/파싱 불가는 None.

        .json 엔드포인트는 콤마 없는 정수 문자열(예: '514531948000000')을 반환하고,
        음수는 마이너스 부호('-123')로 온다. 일부 응답의 괄호 표기도 방어적으로 처리한다.
        """
        if raw is None:
            return None
        s = str(raw).strip().replace(",", "")
        if s in ("", "-", "nan"):
            return None
        neg = s.startswith("(") and s.endswith(")")
        if neg:
            s = s[1:-1]
        try:
            val = int(float(s))
        except (ValueError, TypeError):
            return None
        return -val if neg else val

    @classmethod
    def _request_fnltt(cls, corp_code: str, bsns_year: int, fs_div: str) -> list:
        """fnlttSinglAcntAll 1회 호출. 정상 list 반환, 무자료(013)/오류는 빈 list."""
        resp = requests.get(
            "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
            params={
                "crtfc_key": settings.DART_API_KEY,
                "corp_code": corp_code,
                "bsns_year": str(bsns_year),
                "reprt_code": "11011",   # 사업보고서(연간)
                "fs_div": fs_div,        # CFS=연결, OFS=별도
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "000":
            # "013"(조회 데이터 없음) 포함 — 빈 list로 폴백(다른 fs_div / extract_fs)을 유도
            return []
        return data.get("list", []) or []

    # 공시 목록 당일 캐시 (Phase 98) — corp_code -> (조회일, rows). DART 일일 쿼터 보호.
    _disclosure_cache: dict = {}

    @classmethod
    def fetch_recent_disclosures(cls, corp_code: str) -> list:
        """최근 1년 공시 목록(list.json)을 유형별(A=정기·B=주요사항·I=거래소)로 조회해 병합.

        전체 최신 100건 1회 조회로는 대량 공시 기업(삼성전자 등)의 정기보고서가
        조회 범위 밖으로 밀려나므로 유형별로 나눠 호출한다.
        무자료(013)는 해당 유형만 빈 결과. 같은 날 재호출은 캐시로 응답.
        그 외 비정상 status(쿼터 초과 등)와 네트워크 오류는 예외를 올린다
        (엔드포인트에서 503 처리) — 불완전 결과가 당일 캐시에 저장되지 않도록.
        """
        today = datetime.now().date()
        cached = cls._disclosure_cache.get(corp_code)
        if cached and cached[0] == today:
            return cached[1]
        rows: list = []
        for pblntf_ty in ("A", "B", "I"):
            resp = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={
                    "crtfc_key": settings.DART_API_KEY,
                    "corp_code": corp_code,
                    "bgn_de": (today - timedelta(days=365)).strftime("%Y%m%d"),
                    "end_de": today.strftime("%Y%m%d"),
                    "pblntf_ty": pblntf_ty,
                    "page_no": "1",
                    "page_count": "100",
                    "sort": "date",
                    "sort_mth": "desc",
                    "last_reprt_at": "Y",   # 정정공시는 최종보고서만
                },
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            status = data.get("status")
            if status == "000":
                rows.extend(data.get("list", []) or [])
            elif status != "013":   # 013 = 조회된 데이터 없음 (정상 빈 결과)
                logger.warning(
                    "DART 공시 목록 비정상 status(corp=%s, ty=%s): %s %s",
                    corp_code, pblntf_ty, status, data.get("message"),
                )
                raise RuntimeError(f"DART 공시 조회 실패 (status={status})")
        rows.sort(key=lambda r: str(r.get("rcept_dt", "")), reverse=True)
        cls._disclosure_cache[corp_code] = (today, rows)
        return rows

    @classmethod
    def _fetch_open_api_year(
        cls,
        corp_code: str,
        bsns_year: int,
        concept_mapping: dict,
        years: list,
        extracted: dict,
        name_mapping: dict | None = None,
    ) -> None:
        """한 사업연도 보고서(당기/전기/전전기)를 받아 extracted에 in-place 병합. CFS→OFS 폴백."""
        rows = cls._request_fnltt(corp_code, bsns_year, fs_div="CFS")
        if not rows:
            rows = cls._request_fnltt(corp_code, bsns_year, fs_div="OFS")
        if not rows:
            return

        year_fields = [
            (bsns_year,     "thstrm_amount"),
            (bsns_year - 1, "frmtrm_amount"),
            (bsns_year - 2, "bfefrmtrm_amount"),
        ]
        for row in rows:
            key = concept_mapping.get(row.get("account_id"))
            if not key and name_mapping:
                acc_nm = re.sub(r'\s+', '', str(row.get("account_nm", "")))
                key = name_mapping.get(acc_nm)
            if not key:
                continue
            for year, field in year_fields:
                if year not in extracted:
                    continue
                val = cls._parse_open_api_amount(row.get(field))
                if val is None:
                    continue
                # 동일 지표 중복(BS/IS/CIS) 시 절댓값 큰 값 우선 — _parse_fs_into 규칙과 일치
                if key not in extracted[year] or abs(val) > abs(extracted[year][key]):
                    extracted[year][key] = val

    @classmethod
    def _collect_via_open_api(
        cls,
        corp,
        concept_mapping: dict,
        years: list,
        extracted: dict,
        name_mapping: dict | None = None,
    ) -> None:
        """DART OpenAPI fnlttSinglAcntAll로 재무제표 수집(~1초/호출, fast-path).

        1개 사업보고서가 당기/전기/전전기 3개년을 포함하므로,
        5개년(years = [N-4 .. N])은 최신 보고서(N: N,N-1,N-2) +
        필요 시 과거 보고서(N-2: N-2,N-3,N-4)로 커버한다. extracted를 in-place로 채운다.
        """
        corp_code = getattr(corp, "corp_code", None)
        if not corp_code:
            raise ValueError("corp_code 없음 — OpenAPI fast-path 불가")

        current_year = years[-1]
        cls._fetch_open_api_year(corp_code, current_year, concept_mapping, years, extracted, name_mapping)
        if any(not extracted[y] for y in years):
            cls._fetch_open_api_year(corp_code, current_year - 2, concept_mapping, years, extracted, name_mapping)

    @classmethod
    def _collect_financial_data(
        cls,
        corp,
        concept_mapping: dict,
        years: list,
        name_mapping: dict | None = None,
    ) -> dict:
        """DART에서 재무제표를 수집하고 concept_mapping 기준으로 파싱.
        반환값: {연도: {지표명: 금액(원)}}

        하이브리드: fnlttSinglAcntAll(fast-path, ~1초)를 먼저 시도하고,
        누락/실패한 연도에 대해서만 dart-fss extract_fs로 폴백한다.
        DART 사업보고서 1건에는 당해연도 + 전년도 2개 연도가 포함되므로,
        5개년 수집을 위해 최신 보고서와 과거 보고서를 각각 호출해 병합한다.
        """
        extracted: dict = {y: {} for y in years}
        current_year = years[-1]

        # 1) fast-path: OpenAPI fnlttSinglAcntAll
        try:
            if name_mapping:
                cls._collect_via_open_api(corp, concept_mapping, years, extracted, name_mapping)
            else:
                cls._collect_via_open_api(corp, concept_mapping, years, extracted)
        except Exception as e:
            logger.warning(f"OpenAPI fast-path 실패, extract_fs로 폴백: {e}")

        # 2) fallback: 아직 빈 연도가 있으면 extract_fs로 메움
        if any(not extracted[y] for y in years):
            # 최신 보고서 (최근 2개년 포함)
            try:
                fs_recent = corp.extract_fs(bgn_de=f"{current_year - 1}0101")
                if name_mapping:
                    cls._parse_fs_into(fs_recent, years, concept_mapping, extracted, name_mapping)
                else:
                    cls._parse_fs_into(fs_recent, years, concept_mapping, extracted)
            except Exception as e:
                logger.warning(f"최신 보고서(extract_fs) 폴백 실패: {e}")
                # fast-path도 폴백도 데이터를 못 얻었으면 전파(상위 retry loop가 처리)
                if all(not extracted[y] for y in years):
                    raise

            # 과거 보고서 (3개년 전 기준) — 아직 없는 연도가 남아 있을 때만 호출
            older_years = [y for y in years if not extracted[y]]
            if older_years:
                try:
                    fs_old = corp.extract_fs(bgn_de=f"{years[0]}0101", end_de=f"{current_year - 2}1231")
                    if name_mapping:
                        cls._parse_fs_into(fs_old, older_years, concept_mapping, extracted, name_mapping)
                    else:
                        cls._parse_fs_into(fs_old, older_years, concept_mapping, extracted)
                except Exception as e:
                    logger.warning(f"과거 보고서(extract_fs) 폴백 실패 (bgn={years[0]}): {e}")

        _coalesce_net_income_basis(extracted, years, getattr(corp, "stock_code", "") or getattr(corp, "corp_name", ""))
        return extracted

    @classmethod
    def _apply_corrections(
        cls,
        extracted_data: dict,
        corp_name: str,
        stock_code: str,
        source_report: str,
        is_financial: bool,
    ) -> list:
        """추출된 원시 데이터에 보정 로직을 적용하고 DB 저장용 파라미터 목록 반환."""
        prepared = []
        for year, data in extracted_data.items():
            if not data:
                continue

            if not is_financial:
                rev = data.get('revenue', 0)
                op = data.get('operating_profit', 0)
                sga = data.get('sga', 0)
                cos = data.get('cost_of_sales', 0)
                gp = data.get('gross_profit', 0)

                if gp == 0 and rev != 0:
                    gp = rev - cos
                    data['gross_profit'] = gp

                # sga를 실제로 계산할 수 있을 때만 data에 기록 — 조건 미충족 시 None으로 유지
                if sga == 0:
                    if cos == 0 and rev != 0 and op != 0:
                        data['sga'] = rev - op
                    elif gp != 0 and op != 0:
                        data['sga'] = gp - op

            # 금융업은 수집 대상이 아닌 필드를 None으로 저장 (프론트에서 0과 구분 가능하도록)
            if is_financial:
                prepared.append({
                    "company_id": stock_code,
                    "company_name": corp_name,
                    "fiscal_year": year,
                    "revenue": None,
                    "cost_of_sales": None,
                    "gross_profit": None,
                    "sga": None,
                    "operating_profit": None,
                    "net_income": data.get('net_income'),
                    "total_assets": data.get('total_assets'),
                    "total_liabilities": data.get('total_liabilities'),
                    "equity": data.get('equity'),
                    "cash": data.get('cash'),
                    "net_interest_income": data.get('net_interest_income'),
                    "loan_loss_provision": data.get('loan_loss_provision'),
                    "insurance_liability": data.get('insurance_liability'),
                    "sector_detail": cls.classify_financial_sector(stock_code) or None,
                    "source_report": source_report,
                })
            else:
                # 미수집 필드는 0 대신 None으로 저장 — 분석 함수의 None 체크가 올바르게 동작하도록
                prepared.append({
                    "company_id": stock_code,
                    "company_name": corp_name,
                    "fiscal_year": year,
                    "revenue": data.get('revenue'),
                    "cost_of_sales": data.get('cost_of_sales'),
                    "gross_profit": data.get('gross_profit'),
                    "sga": data.get('sga'),
                    "operating_profit": data.get('operating_profit'),
                    "net_income": data.get('net_income'),
                    "total_assets": data.get('total_assets'),
                    "total_liabilities": data.get('total_liabilities'),
                    "equity": data.get('equity'),
                    "cash": data.get('cash'),
                    "other_income": data.get('other_income'),
                    "other_expense": data.get('other_expense'),
                    "finance_income": data.get('finance_income'),
                    "finance_cost": data.get('finance_cost'),
                    "income_before_tax": data.get('income_before_tax'),
                    "income_tax_expense": data.get('income_tax_expense'),
                    "operating_cash_flow": data.get('operating_cash_flow'),
                    "source_report": source_report,
                })
        return prepared

    @classmethod
    def _get_retry_delays(cls) -> tuple:
        """settings.DART_RETRY_DELAYS를 파싱해 반환. 파싱 실패 시 기본값 사용."""
        try:
            return tuple(int(s.strip()) for s in settings.DART_RETRY_DELAYS.split(",") if s.strip())
        except Exception:
            return (5, 30, 120)

    @classmethod
    def _latest_fiscal_year(cls, now: datetime | None = None) -> int:
        """DART 사업보고서가 안정적으로 공시됐을 가능성이 높은 최신 사업연도.

        12월 결산 법인의 사업보고서 제출 마감은 보통 다음 해 3월 말이므로,
        1~3월에는 직전 연도 보고서 대신 그 전 사업연도를 기준으로 잡는다.
        """
        now = now or datetime.now()
        return now.year - 1 if now.month >= 4 else now.year - 2

    @classmethod
    def fetch_and_process_data(cls, stock_code: str):
        """DART 재무 데이터 수집·저장. 백그라운드 전용 세션 사용, 수집 성공 후 기존 데이터 교체."""
        from app.db.database import SessionLocal
        db = SessionLocal()

        last_error: Exception = RuntimeError("알 수 없는 오류")
        try:
            task = db.query(CollectionTask).filter(
                CollectionTask.stock_code == stock_code
            ).first()

            target_corp = cls._find_corp(stock_code)
            if target_corp is None:
                raise ValueError(
                    f"기업 리스트에서 '{stock_code}'를 찾을 수 없습니다. "
                    "상장폐지 또는 DART 데이터 불일치일 수 있습니다."
                )

            is_financial = cls._is_financial_sector(target_corp)
            concept_mapping = cls.FINANCIAL_CONCEPT_MAPPING if is_financial else cls.CONCEPT_MAPPING
            name_mapping = cls.FINANCIAL_ACCOUNT_NAME_MAPPING if is_financial else cls.GENERAL_ACCOUNT_NAME_MAPPING

            if is_financial:
                logger.info(
                    f"[{target_corp.corp_name}] 금융업 감지 — "
                    f"공통 지표만 수집 (총자산/부채/자기자본/당기순이익/현금)"
                )

            latest_year = cls._latest_fiscal_year()
            years = list(range(latest_year - 4, latest_year + 1))
            source_report = (
                f"{target_corp.corp_name} {latest_year}년 사업보고서 "
                f"[금융업: 총자산·부채·자기자본·당기순이익만 제공]"
                if is_financial
                else f"{target_corp.corp_name} {latest_year}년 사업보고서"
            )

            # DART API 호출은 네트워크 오류 가능 — ValueError(논리 오류)는 재시도하지 않음
            retry_delays = cls._get_retry_delays()
            for attempt, delay in enumerate(retry_delays, start=1):
                try:
                    extracted_data = cls._collect_financial_data(
                        target_corp, concept_mapping, years, name_mapping
                    )
                    break
                except ValueError:
                    raise
                except Exception as e:
                    last_error = e
                    logger.warning(
                        f"DART 수집 실패 [{stock_code}] (시도 {attempt}/{len(retry_delays)}): {e}. "
                        f"{delay}초 후 재시도..."
                    )
                    time.sleep(delay)
            else:
                raise last_error

            prepared_params = cls._apply_corrections(
                extracted_data, target_corp.corp_name, stock_code, source_report, is_financial
            )

            if not prepared_params:
                raise ValueError("DART에서 유효한 재무 데이터를 추출하지 못했습니다.")

            if task is None:
                raise ValueError(f"CollectionTask({stock_code})가 DB에서 삭제되어 상태를 업데이트할 수 없습니다.")

            db.query(FinancialStatement).filter(
                FinancialStatement.company_id == stock_code
            ).delete()
            db.query(AIAnalysisCache).filter(AIAnalysisCache.stock_code == stock_code).delete()
            for params in prepared_params:
                db.add(FinancialStatement(**params))
            task.status = "completed"
            task.message = f"{target_corp.corp_name} 수집 및 분석 완료"
            db.commit()

        except Exception as e:
            db.rollback()
            logger.error(f"수집 실패 [{stock_code}]: {str(e)}")
            try:
                task = db.query(CollectionTask).filter(
                    CollectionTask.stock_code == stock_code
                ).first()
                if task:
                    task.status = "failed"
                    task.message = str(e)
                    db.commit()
            except Exception:
                db.rollback()
                logger.error(f"태스크 상태 업데이트도 실패 [{stock_code}]")
        finally:
            db.close()
