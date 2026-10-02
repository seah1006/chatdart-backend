"""OpenAI ChatCompletion 기반 요약 - AI팀(skyhubstk/ai-server) 이식."""
import json
import logging
import re
from decimal import Decimal, ROUND_HALF_UP

from openai import OpenAI

from app.core.config import settings

_client: OpenAI | None = None
logger = logging.getLogger(__name__)


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = settings.OPENAI_API_KEY
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY 가 설정되지 않았습니다.")
        _client = OpenAI(api_key=api_key, timeout=15.0)
    return _client


def summarize(
    data,
    *,
    base_year: int,
    forecast_year: int,
    prediction_display_text: str,
    interpretation_points=None,
):
    """AI팀 원본 프롬프트 보존 + 연도·예측값 사실성 계약."""
    flags_block = ""
    if interpretation_points:
        lines = "\n".join(
            f"- {point.title}: {point.message}"
            for point in interpretation_points
        )
        flags_block = (
            "\n주의해서 해석해야 할 재무 신호(반드시 요약에 반영):\n"
            f"{lines}\n"
            "위 주의 신호가 있으면 '좋다/나쁘다'로 단정하지 말고 "
            "'확인이 필요하다', '가능성이 있다'처럼 신중하게 안내하라.\n"
            "영업이익과 당기순이익의 차이를 비전공자도 이해하기 쉽게 설명하라.\n"
        )
    prompt = f'''
기업:{data['company']}
기준연도(실적·현재 상태 설명용): {base_year}년
예측 대상 연도(예측 설명용): {forecast_year}년
예측 표시 문구(그대로 사용): {prediction_display_text}

[연도·예측값 사실성 계약]
실적과 현재 상태를 설명할 때는 기준연도를 사용하고, 예측값을 설명할 때는 반드시 예측 대상 연도를 사용한다.
예측 문장에는 백엔드가 제공한 예측 표시 문구를 그대로 사용하며 연도·지표명·금액·부호·단위를 바꾸지 않는다.
제공되지 않은 연도나 금액을 만들거나 계산·추정·반올림·환산하지 않는다.
예측 표시 문구는 하나의 고정 문자열이다. 글자·띄어쓰기·어순을 바꾸거나 문구 내부에 조사를 넣지 말고, 문장에 필요한 조사는 문구 전체 뒤에 붙인다.
금액을 언급할 때는 반드시 예측 표시 문구 전체를 그대로 사용한다. 같은 문구를 여러 번 사용할 수 있지만, 금액만 떼어 쓰거나 다른 표현으로 다시 쓰지 말고 각 문장에 예측 대상 연도를 명시한다.
맞음: 2026년에는 예상 영업이익 약 12.6조 원을 기록할 것으로 보입니다.
틀림: 2026년에는 예상 영업이익이 약 12.6조 원에 이를 것으로 보입니다.

매출 추세:{data['trend']}
부채 위험:{data['risk']}
{flags_block}

초보자도 이해할 수 있게
3줄 요약
장점3개
위험3개
'''
    res = _get_client().chat.completions.create(
        model='gpt-4o-mini',
        messages=[{'role': 'user', 'content': prompt}],
    )
    return res.choices[0].message.content


def canonical_prediction(pred: float) -> int:
    return int(Decimal(str(pred)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def build_prediction_display_text(pred: float, is_financial: bool) -> str:
    """예측값을 AI 프롬프트와 API 응답에 사용할 결정적 표시 문구로 만든다."""
    integer_value = canonical_prediction(pred)
    absolute = abs(integer_value)
    if is_financial:
        metric = "당기순손실" if integer_value < 0 else "당기순이익"
    else:
        metric = "영업손실" if integer_value < 0 else "영업이익"

    if absolute < 100_000_000:
        amount = f"{absolute:,}원"
        return f"예상 {metric} {amount}"

    eok = (Decimal(absolute) / Decimal(100_000_000)).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    if absolute >= 1_000_000_000_000 or eok >= 10_000:
        jo = (Decimal(absolute) / Decimal(1_000_000_000_000)).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
        return f"예상 {metric} 약 {jo}조 원"
    return f"예상 {metric} 약 {eok:,}억 원"


# rule ID → check_items 후보 (AI 담당 확정 매핑, 2026-07-19).
# 법인세비용은 음수면 법인세수익일 수 있어 보수적으로 제외 (AI 담당도 초기 3개 구성 우선 권장).
_RULE_CHECK_ITEMS = {
    # warning
    "operating-loss-net-profit": ("기타수익", "금융수익", "영업활동현금흐름"),
    "revenue-up-operating-profit-down": ("매출원가", "판매비와관리비"),
    "operating-margin-sharp-decline": ("매출원가", "판매비와관리비"),
    "net-income-much-higher-than-operating-profit": ("기타수익", "금융수익", "영업활동현금흐름"),
    "net-income-positive-operating-cash-flow-negative": ("영업활동현금흐름",),
    "operating-cash-flow-much-lower-than-net-income": ("영업활동현금흐름",),  # Phase 107 (AI A3)
    # positive
    "revenue-and-operating-profit-up": ("매출원가", "판매비와관리비", "영업활동현금흐름"),
    "operating-margin-improved": ("매출원가", "판매비와관리비", "영업활동현금흐름"),
    "operating-profit-turnaround": ("매출원가", "판매비와관리비", "영업활동현금흐름"),
    "operating-profit-and-net-income-up": ("매출원가", "판매비와관리비", "기타수익", "금융수익", "영업활동현금흐름"),
    "operating-cash-flow-turnaround": ("영업활동현금흐름",),
}

# insights/interpret 프롬프트 버전. 프롬프트 규칙 변경 시 반드시 +1 (캐시 무효화).
# v8은 2026-09-02 운영 배포됐다.
# v9는 A안(R18)과 R04·R17 표현 변경을 반영해 2026-09-10 운영 배포됐다.
# v10은 2026-09-10 배포됐으며 R18의 전환 표현 제거와 R17 승인 예시 교체를 반영한다.
# v11은 headline의 primary_flag 고정과 R17 조건절을 반영한다.
# v12는 B6 A안(R16·R19 꼬리절 개정)과 데이터 기준연도 표기를 반영한다.
# 배포 이후 규칙 변경이므로 버전을 올려 이전 프롬프트의 캐시와 구분한다.
INSIGHT_PROMPT_VERSION = 12


def build_check_items(warning_flags, positive_flags) -> list:
    """rule ID 기반 check_items 결정적 합성 (AI 회신 2026-07-19 처리 순서).

    warning → positive를 각각 요청 배열 순서대로 처리, rule별 항목 순서 유지,
    중복은 최초 순서 유지로 제거, 최대 5개. 매핑에 없는 ID는 건너뛴다
    (화이트리스트 400이 선행하므로 정상 흐름에서는 도달하지 않는 방어 처리).
    """
    seen = []
    for flag in list(warning_flags) + list(positive_flags):
        for item in _RULE_CHECK_ITEMS.get(flag.id, ()):
            if item not in seen:
                seen.append(item)
            if len(seen) == 5:
                return seen
    return seen


def select_primary_flag(flags):
    """최신 연도의 flag를 고르고, 동률이면 전달 순서의 첫 flag를 반환한다."""
    if not flags:
        return None
    years = [flag.year for flag in flags if flag.year is not None]
    if not years:
        return flags[0]
    latest_year = max(years)
    return next(flag for flag in flags if flag.year == latest_year)


def _headline_year(headline):
    match = re.search(r"^\s*(\d{4})\s*년", headline or "")
    return int(match.group(1)) if match else None


def check_headline_year(headline, payload):
    """headline 선두 연도와 primary flag·다른 flag 연도 혼입을 검증한다."""
    all_flags = list(payload.warning_flags) + list(payload.positive_flags)
    primary_flag = select_primary_flag(all_flags)
    expected_year = primary_flag.year if primary_flag else None
    actual_year = _headline_year(headline)
    year_mismatch = True
    if expected_year is None:
        # InsightFlag.year 는 int 필수(finance_schema.py)라 API 경로에서는 도달하지 않는다. 방어용.
        year_mismatch = False
    else:
        year_mismatch = actual_year != expected_year
    other_years = {
        flag.year for flag in all_flags
        if flag.year is not None and flag.year != expected_year
    }
    other_year_mentioned = any(f"{year}년" in (headline or "") for year in other_years)
    return {
        "expected_year": expected_year,
        "actual_year": actual_year,
        "primary_rule": primary_flag.id if primary_flag else None,
        "primary_title": (
            primary_flag.title.replace("\r", " ").replace("\n", " ")
            if primary_flag else None
        ),
        "year_mismatch": year_mismatch,
        "other_year_mentioned": other_year_mentioned,
        "mismatch": year_mismatch or other_year_mentioned,
        "cache_blocking": year_mismatch,
    }


def _display_value(value, unit: str = "") -> str:
    """AI 회신(2026-07-15) 반영: 큰 금액은 백엔드가 표시용 값으로 변환해 전달한다.
    비율 항목(영업이익률 등)은 1억 미만이라 원본 값에 unit(%)만 붙는다."""
    if value is None:
        return "정보 없음"
    if abs(value) >= 1e12:
        return f"약 {value / 1e12:.1f}조 원"
    if abs(value) >= 1e8:
        return f"약 {value / 1e8:,.0f}억 원"
    return f"{value}{unit}"


def _sign_label(value) -> str:
    """전환/유지 오판 방지용 부호 상태 (AI 회신 2026-07-18). None은 '정보 없음'."""
    if value is None:
        return "정보 없음"
    if value > 0:
        return "양수"
    if value < 0:
        return "음수"
    return "0"


def _subject_josa(name: str) -> str:
    """회사명 끝 글자의 받침 여부로 주격조사 은/는을 결정한다 (Phase 108).

    한글 음절이 받침을 가지면 '은', 없으면 '는'. 한글이 아닌 끝 글자(영문·기호)는
    '는'으로 둔다. 영문·약어 사명은 한글 발음 기준이 아니라 이 기본값으로 떨어지므로
    틀릴 수 있다 (예: S-Oil→'S-Oil는', 실제로는 '에쓰오일은'이 맞다). MVP 범위 밖으로 둔다.
    headline 지시문의 하드코딩된 '는'이 유발한 조사 오류('LG화학는') 수정용.

    끝의 닫는 괄호는 소리내어 읽지 않으므로 걷어낸 뒤 판정한다 (Phase 110, AI 회신 7절).
    '합성테스트기업(샘플)' → '플'의 받침 ㄹ → '은'.
    공백 제거는 이 함수가 하지 않는다 — 호출부에서 회사명을 strip 한 뒤 넘긴다.
    """
    if not name:
        return "는"
    stripped = name.rstrip(")]}>）］》")
    if not stripped:
        return "는"
    code = ord(stripped[-1])
    if 0xAC00 <= code <= 0xD7A3:  # 한글 음절 영역
        return "은" if (code - 0xAC00) % 28 else "는"
    return "는"


def _format_insight_flags(flags: list) -> str:
    if not flags:
        return "(없음)"
    lines = []
    for flag in flags:
        lines.append(f"- [{flag.year}] {flag.title}")
        for ev in flag.evidence:
            unit = "%" if ev.label.endswith(("률", "율")) else ""
            if ev.previousValue is None:
                # 전년 값이 없는데 "전년 정보 없음 → 당해 X"로 쓰면 화살표가 추세로 읽혀
                # 모델이 "유지했다"·"증가했다" 같은 없는 비교를 만든다 (Phase 109, AI 회신 3·4절).
                # 전년 값이 없으면 변화율도 정의되지 않으므로 붙이지 않는다.
                lines.append(
                    f"  · {ev.label}: 당해 {_display_value(ev.currentValue, unit)}"
                    " (전년 값 없음 — 비교·추세 표현 금지)"
                    f" [부호: 당해 {_sign_label(ev.currentValue)}]"
                )
                continue
            line = (
                f"  · {ev.label}: 전년 {_display_value(ev.previousValue, unit)}"
                f" → 당해 {_display_value(ev.currentValue, unit)}"
            )
            if ev.changeRate is not None:
                line += f" (변화율 {ev.changeRate}%)"
            line += f" [부호: 전년 {_sign_label(ev.previousValue)} → 당해 {_sign_label(ev.currentValue)}]"
            lines.append(line)
    return "\n".join(lines)


def _log_headline_year_verdict(verdict, request_id, payload, raw_response):
    log_fields = (
        request_id,
        payload.stock_code,
        INSIGHT_PROMPT_VERSION,
        verdict["primary_rule"],
        verdict["expected_year"],
        verdict["actual_year"],
        verdict["primary_title"],
    )
    if verdict["mismatch"]:
        logger.warning(
            "AI headline 연도 불일치 request_id=%s stock_code=%s prompt_version=%s "
            "primary_rule=%s expected_year=%s actual_year=%s primary_title=%s "
            "year_mismatch=%s other_year_mentioned=%s raw_response=%s payload=%s",
            *log_fields,
            verdict["year_mismatch"],
            verdict["other_year_mentioned"],
            raw_response,
            payload.model_dump_json(),
        )
    else:
        logger.info(
            "AI headline 연도 검증 통과 request_id=%s stock_code=%s prompt_version=%s "
            "primary_rule=%s expected_year=%s actual_year=%s primary_title=%s",
            *log_fields,
        )


def interpret_insights(payload, request_id=None) -> dict:
    """rule 기반 재무 신호를 자연어로 해석 (Phase 97). OpenAI 1회 호출, JSON 출력 강제.

    payload: InsightInterpretRequest. 파싱 실패·headline 누락 시 예외를 올린다 (엔드포인트에서 폴백).
    """
    # company에는 pattern 제약이 없어(finance_schema.py: max_length=50만) 후행 공백·개행이
    # 그대로 도달한다. 조사 판정과 프롬프트 표기가 어긋나지 않도록 여기서 한 번만 정규화한다.
    company = payload.company.strip()
    company_josa = _subject_josa(company)
    primary_flag = select_primary_flag(payload.warning_flags + payload.positive_flags)
    primary_flag_block = "primary_flag: 없음"
    headline_format = f'"{company}{company_josa} ..."'
    if primary_flag is not None:
        primary_title = primary_flag.title.replace("\r", " ").replace("\n", " ")
        primary_flag_block = (
            f"primary_flag: rule={primary_flag.id}; year={primary_flag.year}; "
            f"title={primary_title}"
        )
        if primary_flag.year is not None:
            headline_format = f'"{primary_flag.year}년 {company}{company_josa} ..."'
    prompt = f"""다음은 {company}의 재무제표(데이터 기준연도 {payload.latest_year}년)에서 rule 기반으로 탐지된 신호다. 데이터 기준연도를 headline 연도로 자동 사용하지 말고, headline의 연도는 아래 primary_flag의 연도와 headline 형식에 따라 정한다.

주의해서 볼 신호:
{_format_insight_flags(payload.warning_flags)}

긍정적으로 볼 신호:
{_format_insight_flags(payload.positive_flags)}

headline에 요약할 신호:
{primary_flag_block}
headline 형식:
{headline_format}

위 신호를 재무 초보자도 이해할 수 있는 한국어 문장으로 해석하라.

반드시 지킬 규칙:
- 규칙이 충돌해 보이면 다음 순서로 적용하라. (1) 하드 출력 계약과 evidence·사실성·안전성 규칙이 가장 우선이다 — 아래 JSON 스키마와 각 필드의 의미, positive와 caution 각각 최대 2문장 상한, 전달된 표시용 값의 숫자와 단위 보존(임의로 다시 계산하거나 환산하지 않기), 비율 evidence의 전달된 백분율 값 그대로 사용, null 조건과 지표별 용어 구분(이익 항목은 흑자·적자, 영업활동현금흐름은 양수·음수), 부호와 지표 의미를 다른 항목으로 옮겨 쓰지 않기, 제공되지 않은 수치 생성 금지, 원인 단정 금지와 투자 판단 금지가 여기에 속한다. (2) 그 계약을 위반하지 않는 범위에서 개별 신호 전용 규칙을 적용하고, 개별 신호 규칙에 명시된 필수 의미와 허용된 추가 확인 방향을 우선한다. (3) 그다음 일반 문체·표현 선호 규칙을 적용한다. 개별 신호 전용 규칙이 (1)의 하드 계약이나 evidence·사실성·안전성 규칙과 충돌하면 개별 신호 규칙을 따르지 말고 안전한 사실 표현을 사용하라.
- 위에 전달된 신호와 수치만 설명하고, 제공되지 않은 수치를 추측하거나 만들어내지 마라. 전달된 evidence에 없는 중간 단계나 원인 개념을 새로 만들어 쓰지도 마라. 제공되지 않은 계정이나 개념을 이미 확인된 사실 또는 발생 원인처럼 서술하지 마라. 다만 아래 개별 신호 규칙이 그 신호에 대해 확인 방향으로 명시적으로 허용한 항목은, 해당 신호를 설명할 때에 한해 추가로 확인할 방향으로만 안내할 수 있다. 허용된 확인 방향도 실제 원인인 것처럼 쓰지 말고 "확인할 필요가 있습니다" 수준으로만 표현하라.
- 금액은 위에 제공된 표시용 값(약 X조 원, 약 X억 원)을 그대로 사용하고, 원 단위 전체 숫자로 되돌리거나 직접 단위를 환산하지 마라.
- 원인을 단정하지 마라. "~때문입니다"처럼 원인을 확정하지 말고, 가능성 수준으로 표현하라.
- "위험합니다", "투자 가치", "매수", "매도" 같은 투자 판단 표현을 쓰지 마라.
- 적자에서 흑자로 바뀐 경우 음수 금액에서 증가했다고 쓰지 말고 "전년 적자에서 흑자로 전환했습니다"처럼 변화의 의미 중심으로 표현하라. 필요하면 당해 표시용 값을 덧붙여라.
- 주의 신호를 설명할 때는 전달된 변화율이나 전년→당해 값을 포함해 왜 확인이 필요한지 보여줘라. 값이 "정보 없음"이거나 "전년 값 없음"으로 표기됐으면 그 전년 수치와 비교 표현은 생략하고, 당해 값은 그대로 사용하라.
- "(전년 값 없음 — 비교·추세 표현 금지)"으로 표기된 항목은 당해 값만 사실대로 서술하고 "유지", "증가", "감소", "개선", "악화", "전환", "지속" 같은 비교·추세 표현을 어떤 형태로도 쓰지 마라. "흑자를 유지했습니다", "흑자를 유지했지만"도 금지다. 또한 "양호", "견조", "안정적", "우수" 같은 평가 표현은 전달된 수치만으로 판단할 기준이 없으므로 쓰지 마라.
- 반대로 전년 값이 표기돼 있고 변화율만 전달되지 않은 항목은, 전년·당해 값에서 직접 확인되는 방향과 부호 전환을 설명해도 된다("전년 적자에서 당해 흑자로 전환했습니다", "전년 음수에서 당해 양수로 전환했습니다"). 다만 전달되지 않은 정확한 증감률을 만들어 "전년 대비 X% 증가했습니다"처럼 쓰지는 마라.
- 당기순이익·영업이익 같은 이익 항목을 "양호", "우수", "견조", "안정적", "부진"처럼 별도의 판단 기준이 필요한 표현으로 평가하지 마라. 전달된 값과 부호, 전달된 변화율만 사실대로 서술하라.
- 변화율은 전달된 확정 값이므로 "약"을 붙이지 마라. 금액 표시용 값(약 X조 원, 약 X억 원)의 "약"은 유지하라. 표시용 값을 문장에 쓸 때 "약"을 제거하거나 금액을 다른 형태로 다시 쓰지 마라. 문장에 쓰지 않을 값은 수치 자체를 생략하라.
- "변화율 X%를 기록했습니다/보였습니다" 표현을 쓰지 마라. 양수 변화율은 "전년 대비 X% 증가했습니다", 음수 변화율은 절댓값을 사용해 "전년 대비 X% 감소했습니다"로 표현하라.
- positive와 caution은 각각 반드시 2문장 이하로 작성하라. 이 문장 수 상한은 개별 신호 전용 규칙보다 우선하며 어떤 경우에도 초과하지 마라. 여러 evidence를 설명해야 하면 한 문장 안에서 연결해 문장 수를 줄여라. 전달된 모든 수치를 반복하지 말고 신호를 이해하는 데 필요한 핵심 근거만 선택하라.
- 같은 항목의 전년·당해 값이 모두 양수이면 "전환"·"흑자전환"·"흑자로 돌아섰다"라고 표현하지 말고 "흑자를 유지했다"(현금흐름은 "양수 흐름을 유지했다")로 표현하라. 단 "전년 값 없음"으로 표기된 항목에는 이 유지 표현을 적용하지 마라. 전환 표현은 전년 값이 0 이하이고 당해 값이 양수인 같은 항목에만 사용하라. 서로 다른 항목의 부호 변화를 혼동하지 말고, 각 evidence 끝의 [부호: …] 표기를 근거로 판단하라.
- 흑자·적자 표현은 영업이익·당기순이익 같은 이익 항목에만 사용하라. 영업활동현금흐름에는 흑자·적자라는 단어를 절대 쓰지 말고 양수·음수·양수 전환으로만 표현하라. 음수 금액을 문장에 쓸 때는 부호가 붙은 표시용 값을 그대로 쓰지 말고 부호를 뺀 뒤, 이익 항목은 "전년 약 4,500억 원의 적자", 현금흐름은 "약 1.2조 원의 음수"처럼 표현하라 (이 부호 표현 변환만 표시용 값 보존 규칙의 예외로 허용된다).
- headline은 primary_flag 한 건의 rule·year·title에 지정된 신호 내용만 요약한다. 다른 warning·positive 신호는 각 필드의 문장 수와 우선순위 범위 안에서 caution·positive에 설명하며 headline에 합치지 않는다.
- 확인을 권고하는 경우에는 evidence가 확인 대상과 확인 목적을 직접 제공할 때만 그 대상과 목적을 명시한다. evidence에 없는 차이·원인·세부 항목을 만들지 않으며, 확인 목적을 근거 범위 안에서 특정할 수 없으면 확인 권고 문장을 새로 생성하지 않는다.
- 당기순이익은 흑자인데 영업활동현금흐름이 음수인 신호는 caution에서 설명하라. 원인을 특정 항목으로 단정하지 말고, 영업활동현금흐름의 세부 구성과 운전자본 변동을 일반적인 추가 확인 방향으로만 안내하라. 매출채권·재고자산·매입채무처럼 제공되지 않은 값을 확인한 것처럼 표현하지 마라. 이 신호에서는 두 지표의 차이와 영업활동현금흐름의 세부 구성을 확인 방향으로 명시적으로 허용한다. 이 신호는 "당기순이익은 흑자이지만 영업활동현금흐름은 음수로 나타났으므로, 두 지표의 차이와 영업활동현금흐름의 세부 구성을 확인할 필요가 있습니다."라는 문장 형태를 우선 사용하고, 한 문장 안에서 "확인" 표현을 두 번 반복하지 마라.
- 당기순이익은 양수인데 영업활동현금흐름이 당기순이익에 비해 낮게(대략 40% 미만) 나타난 신호는 caution에서 설명하라. 이 신호는 영업활동현금흐름이 음수인 신호와 다르므로 "음수 전환"·"유입이 없다"고 쓰지 말고, 당기순이익에 비해 영업활동현금흐름이 낮게 나타났다는 사실만 설명하라. 단년도 결과를 장기적 특성으로 일반화하지 말고 "해당 연도", "단년도", "추가 확인이 필요"의 의미를 노출하라. 원인을 매출채권·재고자산 등 특정 세부 계정으로 단정하지 말고 "영업활동현금흐름의 세부 구성을 함께 확인할 필요가 있습니다" 수준까지만 안내하며, 다음 기간의 수치도 함께 확인할 필요가 있다는 취지를 포함하라. "현금 창출력이 구조적으로 약함", "구조적인 현금 창출력 저하", "이익의 질이 낮음", "지속적인 현금흐름 악화" 같은 표현은 긍정문·부정문을 가리지 않고 어떤 형태로도 쓰지 마라. 이 신호를 설명할 때는 (1) 영업활동현금흐름이 당기순이익에 비해 낮다는 사실, (2) 해당 연도의 단년도 신호라는 점, (3) 다음 기간의 수치와 영업활동현금흐름의 세부 구성을 함께 확인할 필요가 있다는 점을 반드시 모두 담아라. 다른 주의 신호와 함께 전달돼 문장 수가 모자라면 금액을 생략하더라도 이 신호를 빠뜨리지 말고, 당기순이익 대비 영업활동현금흐름 비율이 낮다는 핵심 의미를 먼저 서술하라. 이때도 caution 2문장 상한은 그대로 지켜라. 다른 주의 신호가 함께 있으면 (1)(2)(3)을 두 문장에 나누지 말고 한 문장으로 합쳐 "해당 연도의 영업활동현금흐름은 당기순이익의 X%로 낮게 나타났으며, 단년도 결과만으로 일반화하기 어려우므로 다음 기간의 수치와 세부 구성을 확인할 필요가 있습니다"처럼 쓰고, 그렇게 남은 한 문장을 다른 주의 신호에 배정하라. 이 신호가 전달되면 caution에서 이 신호의 확인 문안을 우선 설명한다. caution의 2문장 상한 때문에 다른 주의 신호를 함께 설명할 수 없으면 우선순위가 낮은 신호의 자연어 요약은 생략할 수 있다. 생략한 신호를 headline으로 옮기거나 구조화 evidence에서 삭제하지 않는다. 반올림된 두 금액을 나란히 반복하면 그 비율이 전달된 비율과 달라 보이므로, 전달된 비율 값을 우선 사용하고 두 금액을 나란히 반복하지 마라. 다른 주의 신호 없이 이 신호만 주의 신호로 전달된 경우의 권장 문장 형태는 "해당 연도의 영업활동현금흐름은 당기순이익의 X%로 낮게 나타났습니다. 단년도 결과만으로 일반화하기 어려우므로, 다음 기간의 수치와 영업활동현금흐름의 세부 구성을 함께 확인할 필요가 있습니다."이다. 이 2문장 형태는 단독 전달일 때만 쓰고, 다른 주의 신호가 함께 있으면 위의 한 문장 형태를 쓰라. "중간 단계"처럼 전달되지 않은 개념을 만들어 쓰지 마라. 비율 값이 전달되지 않았으면 X% 자리에 비율을 만들어 넣지 말고 "당기순이익에 비해 낮게"로만 서술하라.
- 영업활동현금흐름이 전년 음수 또는 0에서 당해 양수로 전환한 신호는 positive에서 양수 전환 사실을 중심으로 설명하라. headline과 positive에서 "긍정적으로 돌아섰다" 같은 평가적 표현 대신 "전년 음수에서 당해 양수로 전환했습니다"처럼 부호 변화를 명시하라. 단일 기간의 결과만으로 지속적인 개선을 단정하지 말고, "다음 기간에도 양수 흐름이 유지되는지 확인할 필요가 있습니다"라는 취지의 문장을 반드시 포함하라. 전년 음수 값을 문장에 쓸 때는 당해 값에도 "양수"를 명시해 "전년 약 3,500억 원의 음수에서 약 6,200억 원의 양수로 전환했습니다"처럼 쓰고, "약 6,200억 원으로 증가했습니다"처럼 당해 부호를 생략하지 마라.
- "양수 흐름" 표현은 현금흐름에만 사용하라. 영업이익이 전년 0 이하에서 당해 양수로 전환한 신호는 "다음 기간에도 영업이익 흑자가 이어지는지 확인할 필요가 있습니다"라는 취지의 문장을 포함하고, 이익 항목의 지속성은 흑자 유지·흑자 지속으로 표현하라.

다음 키를 가진 JSON 객체만 출력하라:
- "headline": 가장 먼저 볼 변화 요약 (1~2문장, 필수). 위 primary_flag의 rule·year·title 한 묶음에 지정된 신호 내용만 요약하고, headline 형식의 연도·회사명 순서로 시작하며 대괄호는 쓰지 마라. 회사명 바로 뒤 주격조사는 반드시 "{company_josa}"를 사용하고 다른 조사로 바꾸지 마라.
- "positive": 긍정적으로 볼 흐름 (최대 2문장). 긍정 신호가 1건 이상 전달됐으면 headline과 별개로 반드시 채우고, 없을 때만 null
- "caution": 주의해서 볼 흐름 (최대 2문장). 주의 신호가 1건 이상 전달됐으면 headline과 별개로 반드시 채우고, 없을 때만 null"""
    res = _get_client().chat.completions.create(
        model='gpt-4o-mini',
        messages=[{'role': 'user', 'content': prompt}],
        response_format={"type": "json_object"},
    )
    raw_response = res.choices[0].message.content
    parsed = json.loads(raw_response)
    if not parsed.get("headline"):
        raise ValueError("AI 해석 응답에 headline이 없습니다.")
    cache_blocking = False
    try:
        verdict = check_headline_year(parsed["headline"], payload)
        cache_blocking = verdict["cache_blocking"]
        _log_headline_year_verdict(verdict, request_id, payload, raw_response)
    except Exception:
        logger.exception("headline 연도 검증·기록 실패 — 응답은 그대로 반환한다")
    return {
        "headline": str(parsed["headline"]),
        "positive": str(parsed["positive"]) if parsed.get("positive") else None,
        "caution": str(parsed["caution"]) if parsed.get("caution") else None,
        # check_items는 모델 출력 대신 rule ID 기반 결정적 합성 (AI 회신 2026-07-19)
        "check_items": build_check_items(payload.warning_flags, payload.positive_flags),
        "_headline_year_mismatch": cache_blocking,
    }


def summarize_compare(companies: list) -> str:
    """복수 기업 지표를 비교해 순위, 강점, 주의점 인사이트 1건을 생성. OpenAI 1회 호출."""
    lines = []
    for company in companies:
        metrics = company["metrics"]
        lines.append(
            f"- {company['company_name']}({company['fiscal_year']}): "
            f"매출성장률 {metrics.get('revenue_growth_rate')}%, "
            f"영업이익률 {metrics.get('operating_margin')}%, "
            f"순이익률 {metrics.get('net_margin')}%, "
            f"ROE {metrics.get('roe')}%, ROA {metrics.get('roa')}%, "
            f"부채비율 {metrics.get('debt_ratio')}%"
        )
    table = "\n".join(lines)
    prompt = f"""다음은 비교 대상 기업들의 최신 재무 지표다.
{table}

초보자도 이해하기 쉽게 한국어로:
- 3줄 비교 요약
- 종합적으로 가장 안정적인 것으로 보이는 기업과 그 이유
- 각 기업의 가장 큰 강점과 가장 큰 주의점
수치가 null인 항목은 "데이터 없음"으로 보고 단정하지 마라.
좋다/나쁘다 단정 대신 '상대적으로', '가능성이 있다'처럼 신중하게 안내하라."""
    res = _get_client().chat.completions.create(
        model='gpt-4o-mini',
        messages=[{'role': 'user', 'content': prompt}],
    )
    return res.choices[0].message.content
