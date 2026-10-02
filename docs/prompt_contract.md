# insights/interpret 프롬프트 계약

> 재번호 반영(2026-08-28): 도입 Phase는 `phase_renumber_20260828.md` 대응표를 따른다.

이 문서는 `POST /api/v1/finance/insights/interpret`가 사용하는 프롬프트의 현재 계약을 한눈에 확인하기 위한 정본이다. 프롬프트를 변경하는 Phase는 같은 Phase 안에서 이 문서와 드리프트 가드 테스트도 함께 갱신한다.

- 출처: `app/services/ai_summary_service.py::interpret_insights()`
- 고정 테스트 출처: `tests/test_insights_interpret.py`
- 프롬프트 버전: `INSIGHT_PROMPT_VERSION = 12`

## 우선순위 tier

프롬프트의 첫 규칙이 선언한 적용 순서는 다음과 같다.

1. **tier 1 — 하드 출력 계약과 evidence·사실성·안전성 규칙**: 아래 JSON 스키마와 각 필드의 의미, positive와 caution 각각 최대 2문장 상한, 전달된 표시용 값의 숫자와 단위 보존(임의로 다시 계산하거나 환산하지 않기), 비율 evidence의 전달된 백분율 값 그대로 사용, null 조건과 지표별 용어 구분(이익 항목은 흑자·적자, 영업활동현금흐름은 양수·음수), 부호와 지표 의미를 다른 항목으로 옮겨 쓰지 않기, 제공되지 않은 수치 생성 금지, 원인 단정 금지와 투자 판단 금지.
2. **tier 2 — 개별 신호 전용 규칙**: tier 1 계약을 위반하지 않는 범위에서 적용하고, 개별 신호 규칙에 명시된 필수 의미와 허용된 추가 확인 방향을 우선한다.
3. **tier 3 — 일반 문체·표현 선호 규칙**.

충돌 계약: "개별 신호 전용 규칙이 (1)의 하드 계약이나 evidence·사실성·안전성 규칙과 충돌하면 개별 신호 규칙을 따르지 말고 안전한 사실 표현을 사용하라."

## 규칙 표

`규칙 요약` 열은 요약 과정에서 리터럴 계약의 의미가 달라지지 않도록 현재 프롬프트의 각 규칙을 원문 그대로 인용한다. `도입 Phase`의 괄호 안 Phase는 후속 보강 이력이다.

| ID | 규칙 요약 | tier | 고정 테스트 | 도입 Phase |
|---|---|---|---|---|
| R01 | 규칙이 충돌해 보이면 다음 순서로 적용하라. (1) 하드 출력 계약과 evidence·사실성·안전성 규칙이 가장 우선이다 — 아래 JSON 스키마와 각 필드의 의미, positive와 caution 각각 최대 2문장 상한, 전달된 표시용 값의 숫자와 단위 보존(임의로 다시 계산하거나 환산하지 않기), 비율 evidence의 전달된 백분율 값 그대로 사용, null 조건과 지표별 용어 구분(이익 항목은 흑자·적자, 영업활동현금흐름은 양수·음수), 부호와 지표 의미를 다른 항목으로 옮겨 쓰지 않기, 제공되지 않은 수치 생성 금지, 원인 단정 금지와 투자 판단 금지가 여기에 속한다. (2) 그 계약을 위반하지 않는 범위에서 개별 신호 전용 규칙을 적용하고, 개별 신호 규칙에 명시된 필수 의미와 허용된 추가 확인 방향을 우선한다. (3) 그다음 일반 문체·표현 선호 규칙을 적용한다. 개별 신호 전용 규칙이 (1)의 하드 계약이나 evidence·사실성·안전성 규칙과 충돌하면 개별 신호 규칙을 따르지 말고 안전한 사실 표현을 사용하라. | 우선순위 선언 | `test_interpret_insights_prompt_declares_rule_priority_order` | 118 (119 보강) |
| R02 | 위에 전달된 신호와 수치만 설명하고, 제공되지 않은 수치를 추측하거나 만들어내지 마라. 전달된 evidence에 없는 중간 단계나 원인 개념을 새로 만들어 쓰지도 마라. 제공되지 않은 계정이나 개념을 이미 확인된 사실 또는 발생 원인처럼 서술하지 마라. 다만 아래 개별 신호 규칙이 그 신호에 대해 확인 방향으로 명시적으로 허용한 항목은, 해당 신호를 설명할 때에 한해 추가로 확인할 방향으로만 안내할 수 있다. 허용된 확인 방향도 실제 원인인 것처럼 쓰지 말고 "확인할 필요가 있습니다" 수준으로만 표현하라. | tier 1 | `test_interpret_insights_prompt_forbids_trend_without_previous_value` | 97 (110·118·137 보강) |
| R03 | 금액은 위에 제공된 표시용 값(약 X조 원, 약 X억 원)을 그대로 사용하고, 원 단위 전체 숫자로 되돌리거나 직접 단위를 환산하지 마라. | tier 1 | `test_prompt_pins_r03_display_value_preservation` | — |
| R04 | 원인을 단정하지 마라. "~때문입니다"처럼 원인을 확정하지 말고, 가능성 수준으로 표현하라. | tier 1 | `test_prompt_pins_r04_no_causal_certainty` | 97 (140 보강) |
| R05 | "위험합니다", "투자 가치", "매수", "매도" 같은 투자 판단 표현을 쓰지 마라. | tier 1 | `test_prompt_pins_r05_no_investment_judgement` | 97 |
| R06 | 적자에서 흑자로 바뀐 경우 음수 금액에서 증가했다고 쓰지 말고 "전년 적자에서 흑자로 전환했습니다"처럼 변화의 의미 중심으로 표현하라. 필요하면 당해 표시용 값을 덧붙여라. | tier 1 | `test_prompt_pins_r06_deficit_to_profit_turnaround` | — |
| R07 | 주의 신호를 설명할 때는 전달된 변화율이나 전년→당해 값을 포함해 왜 확인이 필요한지 보여줘라. 값이 "정보 없음"이거나 "전년 값 없음"으로 표기됐으면 그 전년 수치와 비교 표현은 생략하고, 당해 값은 그대로 사용하라. | tier 1 | `test_prompt_pins_r07_caution_evidence_basis` | — (109 보강) |
| R08 | "(전년 값 없음 — 비교·추세 표현 금지)"으로 표기된 항목은 당해 값만 사실대로 서술하고 "유지", "증가", "감소", "개선", "악화", "전환", "지속" 같은 비교·추세 표현을 어떤 형태로도 쓰지 마라. "흑자를 유지했습니다", "흑자를 유지했지만"도 금지다. 또한 "양호", "견조", "안정적", "우수" 같은 평가 표현은 전달된 수치만으로 판단할 기준이 없으므로 쓰지 마라. | tier 1 | `test_interpret_insights_prompt_forbids_trend_without_previous_value`<br>`test_interpret_insights_prompt_keeps_trend_ban_without_exception` | 109 (119 보강) |
| R09 | 반대로 전년 값이 표기돼 있고 변화율만 전달되지 않은 항목은, 전년·당해 값에서 직접 확인되는 방향과 부호 전환을 설명해도 된다("전년 적자에서 당해 흑자로 전환했습니다", "전년 음수에서 당해 양수로 전환했습니다"). 다만 전달되지 않은 정확한 증감률을 만들어 "전년 대비 X% 증가했습니다"처럼 쓰지는 마라. | tier 1 | `test_interpret_insights_prompt_forbids_trend_without_previous_value` | 110 |
| R10 | 당기순이익·영업이익 같은 이익 항목을 "양호", "우수", "견조", "안정적", "부진"처럼 별도의 판단 기준이 필요한 표현으로 평가하지 마라. 전달된 값과 부호, 전달된 변화율만 사실대로 서술하라. | tier 1 | `test_interpret_insights_prompt_forbids_trend_without_previous_value` | 110 |
| R11 | 변화율은 전달된 확정 값이므로 "약"을 붙이지 마라. 금액 표시용 값(약 X조 원, 약 X억 원)의 "약"은 유지하라. 표시용 값을 문장에 쓸 때 "약"을 제거하거나 금액을 다른 형태로 다시 쓰지 마라. 문장에 쓰지 않을 값은 수치 자체를 생략하라. | tier 1 | `test_interpret_insights_prompt_and_parsing` | 100 (102 보강) |
| R12 | "변화율 X%를 기록했습니다/보였습니다" 표현을 쓰지 마라. 양수 변화율은 "전년 대비 X% 증가했습니다", 음수 변화율은 절댓값을 사용해 "전년 대비 X% 감소했습니다"로 표현하라. | tier 3 | `test_interpret_insights_prompt_reflects_phase102_rules` | 100 (102 보강) |
| R13 | positive와 caution은 각각 반드시 2문장 이하로 작성하라. 이 문장 수 상한은 개별 신호 전용 규칙보다 우선하며 어떤 경우에도 초과하지 마라. 여러 evidence를 설명해야 하면 한 문장 안에서 연결해 문장 수를 줄여라. 전달된 모든 수치를 반복하지 말고 신호를 이해하는 데 필요한 핵심 근거만 선택하라. | tier 1 | `test_interpret_insights_prompt_reflects_phase102_rules`<br>`test_interpret_insights_prompt_declares_rule_priority_order` | 102 (118·119 보강) |
| R14 | 같은 항목의 전년·당해 값이 모두 양수이면 "전환"·"흑자전환"·"흑자로 돌아섰다"라고 표현하지 말고 "흑자를 유지했다"(현금흐름은 "양수 흐름을 유지했다")로 표현하라. 단 "전년 값 없음"으로 표기된 항목에는 이 유지 표현을 적용하지 마라. 전환 표현은 전년 값이 0 이하이고 당해 값이 양수인 같은 항목에만 사용하라. 서로 다른 항목의 부호 변화를 혼동하지 말고, 각 evidence 끝의 [부호: …] 표기를 근거로 판단하라. | tier 1 | `test_interpret_insights_prompt_reflects_phase102_rules`<br>`test_interpret_insights_prompt_forbids_trend_without_previous_value` | 102 (109 보강) |
| R15 | 흑자·적자 표현은 영업이익·당기순이익 같은 이익 항목에만 사용하라. 영업활동현금흐름에는 흑자·적자라는 단어를 절대 쓰지 말고 양수·음수·양수 전환으로만 표현하라. 음수 금액을 문장에 쓸 때는 부호가 붙은 표시용 값을 그대로 쓰지 말고 부호를 뺀 뒤, 이익 항목은 "전년 약 4,500억 원의 적자", 현금흐름은 "약 1.2조 원의 음수"처럼 표현하라 (이 부호 표현 변환만 표시용 값 보존 규칙의 예외로 허용된다). | tier 1 | `test_interpret_insights_prompt_reflects_phase104_rules` | 102 (104 보강) |
| R16 | headline은 primary_flag 한 건의 rule·year·title에 지정된 신호 내용만 요약한다. 다른 warning·positive 신호는 각 필드의 문장 수와 우선순위 범위 안에서 caution·positive에 설명하며 headline에 합치지 않는다. | tier 1 | `test_interpret_insights_prompt_and_parsing` | 100 (163 개정) |
| R17 | 확인을 권고하는 경우에는 evidence가 확인 대상과 확인 목적을 직접 제공할 때만 그 대상과 목적을 명시한다. evidence에 없는 차이·원인·세부 항목을 만들지 않으며, 확인 목적을 근거 범위 안에서 특정할 수 없으면 확인 권고 문장을 새로 생성하지 않는다. | tier 3 | `test_interpret_insights_prompt_and_parsing` | 146 |
| R18 | 당기순이익은 흑자인데 영업활동현금흐름이 음수인 신호는 caution에서 설명하라. 원인을 특정 항목으로 단정하지 말고, 영업활동현금흐름의 세부 구성과 운전자본 변동을 일반적인 추가 확인 방향으로만 안내하라. 매출채권·재고자산·매입채무처럼 제공되지 않은 값을 확인한 것처럼 표현하지 마라. 이 신호에서는 두 지표의 차이와 영업활동현금흐름의 세부 구성을 확인 방향으로 명시적으로 허용한다. 이 신호는 "당기순이익은 흑자이지만 영업활동현금흐름은 음수로 나타났으므로, 두 지표의 차이와 영업활동현금흐름의 세부 구성을 확인할 필요가 있습니다."라는 문장 형태를 우선 사용하고, 한 문장 안에서 "확인" 표현을 두 번 반복하지 마라. | tier 2 | `test_interpret_insights_prompt_reflects_phase101_rules`<br>`test_interpret_insights_prompt_reflects_phase105_wording`<br>`test_interpret_insights_prompt_forbids_trend_without_previous_value` | 101 (105·118·140 보강) |
| R19 | 당기순이익은 양수인데 영업활동현금흐름이 당기순이익에 비해 낮게(대략 40% 미만) 나타난 신호는 caution에서 설명하라. 이 신호는 영업활동현금흐름이 음수인 신호와 다르므로 "음수 전환"·"유입이 없다"고 쓰지 말고, 당기순이익에 비해 영업활동현금흐름이 낮게 나타났다는 사실만 설명하라. 단년도 결과를 장기적 특성으로 일반화하지 말고 "해당 연도", "단년도", "추가 확인이 필요"의 의미를 노출하라. 원인을 매출채권·재고자산 등 특정 세부 계정으로 단정하지 말고 "영업활동현금흐름의 세부 구성을 함께 확인할 필요가 있습니다" 수준까지만 안내하며, 다음 기간의 수치도 함께 확인할 필요가 있다는 취지를 포함하라. "현금 창출력이 구조적으로 약함", "구조적인 현금 창출력 저하", "이익의 질이 낮음", "지속적인 현금흐름 악화" 같은 표현은 긍정문·부정문을 가리지 않고 어떤 형태로도 쓰지 마라. 이 신호를 설명할 때는 (1) 영업활동현금흐름이 당기순이익에 비해 낮다는 사실, (2) 해당 연도의 단년도 신호라는 점, (3) 다음 기간의 수치와 영업활동현금흐름의 세부 구성을 함께 확인할 필요가 있다는 점을 반드시 모두 담아라. 다른 주의 신호와 함께 전달돼 문장 수가 모자라면 금액을 생략하더라도 이 신호를 빠뜨리지 말고, 당기순이익 대비 영업활동현금흐름 비율이 낮다는 핵심 의미를 먼저 서술하라. 이때도 caution 2문장 상한은 그대로 지켜라. 다른 주의 신호가 함께 있으면 (1)(2)(3)을 두 문장에 나누지 말고 한 문장으로 합쳐 "해당 연도의 영업활동현금흐름은 당기순이익의 X%로 낮게 나타났으며, 단년도 결과만으로 일반화하기 어려우므로 다음 기간의 수치와 세부 구성을 확인할 필요가 있습니다"처럼 쓰고, 그렇게 남은 한 문장을 다른 주의 신호에 배정하라. 이 신호가 전달되면 caution에서 이 신호의 확인 문안을 우선 설명한다. caution의 2문장 상한 때문에 다른 주의 신호를 함께 설명할 수 없으면 우선순위가 낮은 신호의 자연어 요약은 생략할 수 있다. 생략한 신호를 headline으로 옮기거나 구조화 evidence에서 삭제하지 않는다. 반올림된 두 금액을 나란히 반복하면 그 비율이 전달된 비율과 달라 보이므로, 전달된 비율 값을 우선 사용하고 두 금액을 나란히 반복하지 마라. 다른 주의 신호 없이 이 신호만 주의 신호로 전달된 경우의 권장 문장 형태는 "해당 연도의 영업활동현금흐름은 당기순이익의 X%로 낮게 나타났습니다. 단년도 결과만으로 일반화하기 어려우므로, 다음 기간의 수치와 영업활동현금흐름의 세부 구성을 함께 확인할 필요가 있습니다."이다. 이 2문장 형태는 단독 전달일 때만 쓰고, 다른 주의 신호가 함께 있으면 위의 한 문장 형태를 쓰라. "중간 단계"처럼 전달되지 않은 개념을 만들어 쓰지 마라. 비율 값이 전달되지 않았으면 X% 자리에 비율을 만들어 넣지 말고 "당기순이익에 비해 낮게"로만 서술하라. | tier 2 | `test_interpret_insights_prompt_reflects_phase107_ocf_much_lower_rule`<br>`test_interpret_insights_prompt_forbids_trend_without_previous_value`<br>`test_interpret_insights_prompt_requires_ocf_ratio_caution_elements`<br>`test_interpret_insights_prompt_reflects_mixed_caution_budget_option_b`<br>`test_interpret_insights_prompt_recommended_sentences_drop_banned_phrases`<br>`test_interpret_insights_prompt_keeps_trend_ban_without_exception` | 107 (109·110·118·119 보강, 163 꼬리절 개정) |
| R20 | 영업활동현금흐름이 전년 음수 또는 0에서 당해 양수로 전환한 신호는 positive에서 양수 전환 사실을 중심으로 설명하라. headline과 positive에서 "긍정적으로 돌아섰다" 같은 평가적 표현 대신 "전년 음수에서 당해 양수로 전환했습니다"처럼 부호 변화를 명시하라. 단일 기간의 결과만으로 지속적인 개선을 단정하지 말고, "다음 기간에도 양수 흐름이 유지되는지 확인할 필요가 있습니다"라는 취지의 문장을 반드시 포함하라. 전년 음수 값을 문장에 쓸 때는 당해 값에도 "양수"를 명시해 "전년 약 3,500억 원의 음수에서 약 6,200억 원의 양수로 전환했습니다"처럼 쓰고, "약 6,200억 원으로 증가했습니다"처럼 당해 부호를 생략하지 마라. | tier 2 | `test_interpret_insights_prompt_reflects_phase101_rules`<br>`test_interpret_insights_prompt_reflects_phase102_rules`<br>`test_interpret_insights_prompt_reflects_phase104_rules`<br>`test_interpret_insights_prompt_reflects_phase105_wording` | 101 (102·104·105 보강) |
| R21 | "양수 흐름" 표현은 현금흐름에만 사용하라. 영업이익이 전년 0 이하에서 당해 양수로 전환한 신호는 "다음 기간에도 영업이익 흑자가 이어지는지 확인할 필요가 있습니다"라는 취지의 문장을 포함하고, 이익 항목의 지속성은 흑자 유지·흑자 지속으로 표현하라. | tier 2 | `test_interpret_insights_prompt_reflects_phase104_rules` | 104 |

## JSON 출력 스키마

모델은 다음 세 키를 가진 JSON 객체만 출력한다.

| 필드 | 문장 수 상한 | 의미와 필수 조건 |
|---|---|---|
| `headline` | 1~2문장 | 가장 먼저 볼 변화 요약. 필수이며, primary_flag의 rule·year·title 한 묶음에 지정된 신호 내용만 요약하고, headline 형식의 연도·회사명 순서로 시작한다. 대괄호를 쓰지 않고 회사명 바로 뒤 주격조사는 계산된 `company_josa`를 그대로 사용한다. |
| `positive` | 최대 2문장 | 긍정적으로 볼 흐름. 긍정 신호가 1건 이상 전달됐으면 `headline`과 별개로 반드시 채우고, 없을 때만 `null`이다. |
| `caution` | 최대 2문장 | 주의해서 볼 흐름. 주의 신호가 1건 이상 전달됐으면 `headline`과 별개로 반드시 채우고, 없을 때만 `null`이다. |

## primary_flag 선택 규칙

1. 후보는 `warning_flags + positive_flags` 순서로 합친다. 호출부는 `app/services/ai_summary_service.py`의 `interpret_insights`, 선택 함수는 `select_primary_flag`다.
2. 후보 중 `flag.year`가 가장 최신인 flag를 고른다.
3. 같은 연도이면 warning이 positive보다 우선하고, 각 배열 안에서는 전달된 순서의 첫 flag를 고른다(`build_check_items`와 같은 순서).
4. flag가 없으면 `primary_flag: 없음`으로 표시하고, headline 형식은 연도 없이 회사명으로 시작한다.
5. `year`가 없는 flag는 현재 요청 스키마(`InsightFlag.year: int`, 필수)로는 도달하지 않는다. 코드에는 방어 분기만 있으며, 연도가 있는 flag와 없는 flag가 섞일 때의 선택 동작은 AI 계약에 명시돼 있지 않다.
6. primary_flag 선택과 headline 형식은 모델이 아니라 백엔드(`select_primary_flag`)가 결정적으로 계산한다.
7. headline은 primary_flag 한 건의 신호 내용만 요약하고, 다른 warning·positive 신호는 caution·positive에서 설명한다(R16). R19 꼬리절도 생략한 신호를 headline으로 옮기지 않는다. 2026-09-17 AI 회신 B6 A안으로 확정했다(원장 O66).
