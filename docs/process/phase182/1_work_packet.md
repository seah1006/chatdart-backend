# Phase 182 Work Packet — finance 범위 판정 대칭화 + 포섭된 중복 테스트 삭제 (O124)

기존 승인 영향: **없음.** `app/` 을 **전혀 건드리지 않는다.** 응답 스키마·프롬프트·
`INSIGHT_PROMPT_VERSION`(12)·rate limit 수치·Swagger 설명·404 `detail` 문구 전부 불변.
`_REQUIRES_429` 집합 내용도 불변이다.

| 항목 | 값 |
|---|---|
| 근거 | `docs/review_findings_phase181_20260922.md` §2 (P181-1 Low · P181-2 Informational) · `docs/open_items.md` **O124** |
| 기준 커밋 | 착수 시 `git fetch origin` 으로 확인 (packet 작성 시점 `9e5b64e`) |
| 배포 | **대상 아님** — `app/` 무변경 |
| 선행 | 없음 |
| 실행 방식 | **독립 세션** — 단일 Phase |

---

## 배경

Phase 181 은 **APPROVE** 를 받았다. 리뷰가 남긴 두 건을 이 Phase 에서 닫는다.
원래는 「Minor 2건 이하 단독 Phase 금지」에 따라 다음 packet 에 묶기로 했으나,
**팀장 지시(2026-09-23)로 Phase 182 로 발행한다.**

- **P181-1** — packet 181 작업 2 가 `_documented_endpoints` 만 `finance*.py` 로 넓히고
  `_violations_for_source` 는 `filename == "finance.py"` 정확 일치로 남겨 두었다.
  **이 비대칭은 Claude packet 의 결함이다.** 파일 분할이 glob 을 도입한 이유였는데,
  정작 그 시나리오에서 「429 설명 누락」 가드가 꺼진다.
- **P181-2** — `tests/test_ai_analysis_endpoint.py` 의 `test_ai_analysis_returns_404_when_no_records` 가
  Phase 181 의 파라미터 테스트 `[ai-analysis]` 케이스에 **완전 포섭**됐는데 남아 있다.
  원인은 packet 181 의 파일 개수 제약이다 — **이번 packet 은 범위를 숫자로 제한하지 않는다.**

### 수정 전 기준선 — Claude 실측 (2026-09-23, `9e5b64e`)

`tests/test_api_rate_limit_docs.py` 의 두 함수를 직접 임포트해 합성 소스를 넣었다.
예문: 429 설명 **없는** `@limiter.limit("20/minute")` 엔드포인트 `moved_undocumented`.

| 가드 | 입력 파일명 | 결과 |
|---|---|---|
| `_violations_for_source` | `finance.py` | `required 429 description missing` **1건** |
| `_violations_for_source` | `finance_compare.py` | **`[]` — 누락이 통과한다** ← 결함 |
| `_violations_for_source` | `admin.py` | `[]` — O104 면제(의도) |
| `_documented_endpoints` | `finance.py` + `finance_compare.py` + `admin.py` | `finance.py:kept_documented` · `finance_compare.py:moved_documented` 수집, admin 제외 |

**수정 후에는 둘째 행이 첫째 행과 같아져야 한다.** 나머지 행은 그대로다.

---

## 작업 1 — finance 범위를 하나의 공유 술어로 통일한다 (P181-1)

**파일**: `tests/test_api_rate_limit_docs.py`

### 고치는 법

1. 모듈 수준에 술어 함수 **하나**를 추가한다. 이름은 `_is_finance_router` 로 한다.

   ```python
   def _is_finance_router(filename):
       return filename.startswith("finance") and filename.endswith(".py")
   ```

2. `_violations_for_source` 의 `if filename == "finance.py":` 를
   `if _is_finance_router(filename):` 로 바꾼다.
3. `_documented_endpoints` 의 파일 선택을 **같은 술어**로 바꾼다.

   ```python
   paths = [p for p in sorted(api_dir.glob("*.py")) if _is_finance_router(p.name)]
   assert paths, f"finance 라우터 파일을 찾지 못했다: {api_dir}"
   ```

   `assert paths` 와 그 메시지는 **유지한다.**
4. **두 함수 어디에도 `"finance"` 문자열 리터럴이 남지 않게 한다** — 범위 판정은 술어 한 곳뿐이어야 한다.
   (`_REQUIRES_429` 의 튜플과 `_assert_documented_endpoints` 의 안내 메시지는 이 규칙의 대상이 아니다.)

### 새 테스트 1개 — 분할 파일이 두 가드에서 같은 범위로 판정되는지

테스트 이름: `test_finance_split_file_is_in_scope_for_both_guards` (`tmp_path` 사용).

- `tmp_path/"finance.py"` — 429 설명을 **가진** 엔드포인트 1개
- `tmp_path/"finance_compare.py"` — 429 설명을 **가진** 엔드포인트 1개 + **없는** 엔드포인트 1개
- 단언 ①: `_violations_for_source(<finance_compare.py 소스>, "finance_compare.py")` 가
  **정확히 1건**이고 그 문자열에 `required 429 description missing` 과 **없는 쪽 함수명**이 들어 있다
- 단언 ②: `_documented_endpoints(tmp_path)` 가 두 파일의 **문서화된 2개와 정확히 같은 집합**이다

위 기준선 표의 예문을 그대로 써도 된다. **예문을 바꾸면** 수정 전 트리에서 단언 ①이
실패하는지(= 결함을 재현하는지) 먼저 확인하고 그 출력을 handoff 에 적는다.

### 기존 테스트는 바꾸지 않는다

- `test_finance_guard_rejects_unlisted_missing_429_description` (`"finance.py"`)
- `test_non_finance_router_keeps_missing_429_exemption` (`"admin.py"`)
- `test_documented_endpoint_set_ignores_other_routers`

셋 다 술어 변경 후에도 **그대로 통과해야 한다.** 통과하지 않으면 고치지 말고 handoff 에 적고 멈춘다.

---

## 작업 2 — O104 이행 주석을 갱신한다 (P181-1 후속)

**파일**: `tests/test_api_rate_limit_docs.py` 상단 `_REQUIRES_429` 위 주석(현재 「O104 진행 시 다음 4곳」)과
`_violations_for_source` 안의 O104 한 줄 주석.

- ①·③ 이 이제 **같은 술어**를 가리키므로 **「`_is_finance_router` 를 넓힌다(두 가드가 함께 바뀐다)」 한 항목으로 합친다.**
  ②·④ 는 그대로 둔다. 항목 번호는 새 개수에 맞게 다시 매긴다.
- **분할 시나리오 한 줄을 추가한다**: 「`finance.py` 를 `finance_*.py` 로 나누면 새 파일도 두 가드의 범위에 들어간다.
  옮긴 엔드포인트의 `_REQUIRES_429` 튜플 첫 원소를 새 파일명으로 바꿔야 한다.」
- 함수명·테스트명은 **착수 시점 실제 이름**으로 적는다.
- 「현재 non-finance 의 429 설명은 0개라 무해」 부수 효과 서술은 **유지한다.**

---

## 작업 3 — 포섭된 중복 테스트를 삭제한다 (P181-2)

**파일**: `tests/test_ai_analysis_endpoint.py` — 함수 `test_ai_analysis_returns_404_when_no_records` **하나를 통째로 삭제**한다.

- 포섭 근거: `tests/test_finance_endpoints.py` 의
  `test_summary_endpoints_return_exact_404_detail_when_no_financial_data[ai-analysis]` 가
  같은 엔드포인트 · 같은 `get_resolved_code` 패치 · 레코드 0개에서 **404 + 정확 문구**를 단언한다.
  삭제 대상은 **상태코드만** 단언한다.
- 삭제로 **고아가 되는 import·상수만** 정리한다. `TEST_STOCK_CODE`·`patch` 는 같은 파일의 다른 테스트가 쓰므로
  남는 것이 정상이다 — 실제로 고아가 생기는지 확인하고 결과를 handoff 에 적는다.
- 같은 파일의 **다른 테스트는 건드리지 않는다.**

---

## 제약

- **`app/` 을 전혀 건드리지 않는다.**
- 변경 대상 식별자·파일:
  - `tests/test_api_rate_limit_docs.py` — `_is_finance_router`(신규) · `_violations_for_source` · `_documented_endpoints` · O104 주석 · 새 테스트 1개
  - `tests/test_ai_analysis_endpoint.py` — `test_ai_analysis_returns_404_when_no_records` 삭제
  - 수집 개수가 바뀌면 `README.md` · `CLAUDE.md` · `AGENTS.md` 의 테스트 개수
  - `docs/claude_handoff_phase182.md` (필수 산출물) · `docs/INDEX.md` (재생성)
- **위 목록 밖을 고쳐야만 사양을 지킬 수 있으면** 고치지 말고 handoff 에 그 충돌을 적고 멈춘다.
  Phase 181 처럼 **사실만 인용하고 지나가지 말 것** — 충돌은 충돌로 올린다.
- `_violations_for_source` 의 **다른 검사**(복수 데코레이터·비분당 한도·파싱 실패·수치 불일치)를 건드리지 않는다.
- `_REQUIRES_429` **집합 내용을 바꾸지 않는다.**
- 줄 수 상한을 두지 않는다 — 범위는 위 식별자·파일로 한정된다.
- 로컬 pytest 는 반드시 `DATABASE_URL=sqlite:///:memory:` 를 붙인다.
- OpenAI 실호출 **0회**. 운영 인프라(EC2/RDS) 접속 **금지**. 서버 기동/재기동·배포 금지.
- 인접 코드·스타일을 "개선"하지 않는다.

---

## 검증

```bash
DATABASE_URL=sqlite:///:memory: python -m pytest -q -p no:cacheprovider tests/test_api_rate_limit_docs.py tests/test_ai_analysis_endpoint.py
DATABASE_URL=sqlite:///:memory: python -m pytest -q -m "slow or not slow" --collect-only | tail -3
DATABASE_URL=sqlite:///:memory: python -m pytest -q -m "slow or not slow"
```

### 수집 개수 산술

| 단계 | 증감 | 누계 |
|---|---|---|
| 기준 (Phase 181) | — | 964 |
| 작업 1 — 새 테스트 | +1 | 965 |
| 작업 2 — 주석 | 0 | 965 |
| 작업 3 — 삭제 | −1 | **964** |

**예상 최종값은 964 — 문서 3종은 바뀌지 않는다.** 실측값이 다르면 **문서를 맞추지 말고 원인을 handoff 에 적는다.**

### 변이 검증 (각각 독립 적용 후 복원, SHA-256 기록)

**변이는 전부 `tests/test_api_rate_limit_docs.py` 의 검사 로직(가드 함수·술어)에 적용한다.**
테스트 함수의 단언을 지우거나 약화하는 변이는 **이 목록에 없다** — 추가하지 말 것.

| 변이 | 대상 (유일 특정) | 치환 | 기대 |
|---|---|---|---|
| S1 | `_violations_for_source` 안의 술어 호출 | `if _is_finance_router(filename):` → `if filename == "finance.py":` | **사망** — 새 테스트 단언 ① (수정 전 결함의 재도입) |
| S2 | `_documented_endpoints` 안의 파일 선택 | 술어 필터를 `p.name == "finance.py"` 로 교체 | **사망** — 새 테스트 단언 ② |
| S3 | `_is_finance_router` 본문 | `return filename.endswith(".py")` (접두 조건 제거) | **사망** — `test_non_finance_router_keeps_missing_429_exemption` · `test_documented_endpoint_set_ignores_other_routers` · 실제 라우터 스캔 |
| S4 | `_is_finance_router` 본문 | `return filename == "finance.py"` | **사망** — 새 테스트 (두 가드 동시 축소) |

- 각 변이 행에 **실패한 테스트 이름**과 종료 코드를 적는다. `errors` 가 나오면 사망이 아니라 **무효 변이**로 보고한다.
- 변이마다 해당 파일의 SHA-256 을 **변이 전·변이 후·원복 후** 세 번, **그 행에** 적는다.
- **S1~S4 중 하나라도 생존하면 그 가드는 없는 것이다.** 보강하고, 불가능하면 이유를 적는다. 생존을 감추지 말 것.
- S3 이 실제 라우터 스캔(`test_rate_limit_decorators_match_swagger_429_descriptions`)에서
  몇 건의 위반을 내는지 **건수를 그대로 적는다**(O104 의 28곳과 대조용). 예측값을 맞추려 하지 말 것.

---

## handoff 필수 항목 — `docs/claude_handoff_phase182.md`

`docs/agent_protocol.md` 4절 증거표 형식 — **각 행에 실제 명령과 그 출력**.

1. 작업 1~3 요구사항 증거표
2. **작업 1 핵심 증거**: 위 「수정 전 기준선」 표를 **수정 후 트리에서 다시 돌린 출력** —
   `finance_compare.py` 행이 1건이 됐고 나머지 행이 불변임을 보인다
3. 두 가드 함수에 `"finance"` 리터럴이 남지 않았음을 보이는 `grep` 출력
4. 작업 2 주석이 **착수 시점 실제 이름**과 일치하는지
5. 작업 3 삭제 후 고아 import·상수 확인 결과
6. 수집 개수 실측값과 산술표 대조
7. 변이 S1~S4 표 — before/after/restored SHA-256 (각 64자) + 실패 테스트 이름 + 종료 코드 · S3 위반 건수
8. `git diff --stat -- app/` **빈 출력** · 최종 `git status` clean
9. CI: push 후 **HEAD 의 SHA 로 run 을 골라** 확인. run 이 없으면 **「미실행」**이라고 적고 다른 run 을 대신 집지 않는다
   (`CLAUDE.md` 「공통 규칙」의 `SHA`/`RID` 스니펫)

---

## 완료 기준

- 로컬 `python -m pytest -q -m "slow or not slow"` 전체 통과, 수집 실측값이 문서 3종과 일치
- 해당 커밋의 **CI green** (HEAD SHA 대조)
- `git diff --stat -- app/` **빈 출력**
- `docs/INDEX.md` 재생성 — `git add` **후** `python scripts/gen_docs_index.py` 를 돌려 함께 커밋
- `docs/claude_handoff_phase182.md` 작성
