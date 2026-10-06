# Phase 182 Handoff — finance 범위 판정 대칭화 + 포섭된 중복 테스트 삭제

## 작업 범위

- `docs/codex_work_packet_phase182.md` 작업 1~3 구현.
- `app/` 무변경, OpenAI 실호출·운영 인프라 접속·서버 기동/재기동·배포 없음.
- 기준 확인: `git fetch origin` 후 `da7825a`에서 착수했으며 당시 로컬 HEAD와 `origin/master`가 일치했다.

## 구현 요약

1. `tests/test_api_rate_limit_docs.py`
   - `_is_finance_router(filename)` 공유 술어를 추가했다.
   - `_violations_for_source`와 `_documented_endpoints`가 같은 술어를 사용하도록 바꿨다.
   - `finance_compare.py`가 누락 설명 검사와 문서화 엔드포인트 수집 양쪽에 포함되는 회귀 테스트를 추가했다.
2. `tests/test_api_rate_limit_docs.py`
   - O104 이행 주석을 공유 술어 기준 3개 항목으로 갱신하고 파일 분할 시 튜플 파일명 변경 의무를 명시했다.
3. `tests/test_ai_analysis_endpoint.py`
   - `test_ai_analysis_returns_404_when_no_records`를 삭제했다. 같은 계약은 `tests/test_finance_endpoints.py::test_summary_endpoints_return_exact_404_detail_when_no_financial_data[ai-analysis]`가 더 강하게 고정한다.

Claude 검토 포인트: 공유 술어의 범위가 `finance*.py` 중 `.py` 파일로만 제한되는지, O104 주석의 실제 식별자 이름이 코드와 일치하는지, 삭제한 테스트 외 인접 테스트가 바뀌지 않았는지 확인해 달라.

## 요구사항 증거표

| 요구사항 | 구현·테스트 근거 | 실행 결과 | 상태 |
|---|---|---|---|
| 작업 1 — 공유 술어와 두 가드 연결 | `_is_finance_router`; `test_finance_split_file_is_in_scope_for_both_guards` | 지정 파일 pytest: `51 passed in 4.80s`, 종료 코드 0 | 완료 |
| 작업 1 — 기존 3개 테스트 불변 통과 | `test_finance_guard_rejects_unlisted_missing_429_description`; `test_non_finance_router_keeps_missing_429_exemption`; `test_documented_endpoint_set_ignores_other_routers` | 같은 명령에서 모두 통과 | 완료 |
| 작업 2 — O104 주석 갱신 | 상단 3개 이행 항목, 분할 시나리오 2줄, 함수 내부 O104 주석 | `rg` 결과 실제 이름 `_is_finance_router`, 두 기존 테스트명, `finance_*.py`, `_REQUIRES_429` 튜플 안내 확인 | 완료 |
| 작업 3 — 포섭 테스트 삭제 | 삭제 대상 부재 AST 확인; `patch`와 `TEST_STOCK_CODE` load count 확인 | `deleted_test_present=False`; `patch=57`, `TEST_STOCK_CODE=37` — 고아 import·상수 없음 | 완료 |
| 수집 수 산술 | +1 신규, -1 삭제 | `964 tests collected in 3.72s`; 예상 964와 일치하여 README/CLAUDE/AGENTS 변경 없음 | 완료 |
| 전체 로컬 기준선 | 전체 저장소 | `DATABASE_URL=sqlite:///:memory: python -m pytest -q -m "slow or not slow"` → `964 passed, 5 warnings in 141.65s`, 종료 코드 0 | 완료 |
| S1 — 누락 설명 가드를 `finance.py` 정확 일치로 축소 | `test_finance_split_file_is_in_scope_for_both_guards` | `1 failed, 10 passed`; 실패: 위 테스트; 종료 코드 1 | 완료 |
| S2 — 문서화 수집을 `finance.py`만으로 축소 | `test_finance_split_file_is_in_scope_for_both_guards` | `1 failed, 10 passed`; 실패: 위 테스트; 종료 코드 1 | 완료 |
| S3 — 접두 조건 제거 | 기존 면제·수집·실제 라우터 스캔 | `3 failed, 8 passed`; 실패: `test_rate_limit_decorators_match_swagger_429_descriptions`, `test_non_finance_router_keeps_missing_429_exemption`, `test_documented_endpoint_set_ignores_other_routers`; 실제 라우터 위반 28건; 종료 코드 1 | 완료 |
| S4 — 공유 술어를 `finance.py` 정확 일치로 축소 | `test_finance_split_file_is_in_scope_for_both_guards` | `1 failed, 10 passed`; 실패: 위 테스트; 종료 코드 1 | 완료 |
| `app/` 무변경 | `git diff --stat 9a49b3d^ 9a49b3d -- app/` | 빈 출력 | 완료 |
| CI | 구현 커밋 `9a49b3d1b970f60510f6d667fdea086ed7e6ccdc` | GitHub Actions CI run `35802416181` success, job `106995519395`, 2m56s | 완료 |

## 작업 1 핵심 증거

수정 후 합성 소스를 두 함수에 직접 넣은 출력이다.

```text
finance.py ["finance.py:moved_undocumented decorator=['20/minute'] docs=None: required 429 description missing"]
finance_compare.py ["finance_compare.py:moved_undocumented decorator=['20/minute'] docs=None: required 429 description missing"]
admin.py []
documented [('finance.py', 'kept_documented'), ('finance_compare.py', 'moved_documented')]
```

`finance_compare.py`의 누락이 정확히 1건 검출됐고, `finance.py`와 `admin.py` 결과 및 두 분할 파일의 문서화 엔드포인트 수집 범위가 요구사항과 일치한다.

두 가드 함수의 AST 문자열 상수에서 정확한 `"finance"` 리터럴을 찾은 결과:

```text
_violations_for_source: exact "finance" literals=[]
_documented_endpoints: exact "finance" literals=[]
```

범위 판정 리터럴은 `_is_finance_router` 한 곳에만 있다. `_documented_endpoints`의 기존 assert 메시지 `finance 라우터 파일을 찾지 못했다`는 packet 지시대로 유지했다.

## 변이 검증

명령: 각 변이를 `tests/test_api_rate_limit_docs.py`에 독립 적용하고 `DATABASE_URL=sqlite:///:memory: python -m pytest -q -p no:cacheprovider tests/test_api_rate_limit_docs.py` 실행 후 즉시 복원했다. errors는 없었다.

| 변이 | before SHA-256 | after SHA-256 | restored SHA-256 | 결과 |
|---|---|---|---|---|
| S1 | `f84cea9322097987f7a595476fed86939f6e750c679df4753127de0e4f324bde` | `923ec6fd888dce730d9ae52a40351223c791f734b3ef39066f2499393f0d6e1a` | `f84cea9322097987f7a595476fed86939f6e750c679df4753127de0e4f324bde` | 사망, 신규 분할 테스트 실패, exit 1 |
| S2 | `f84cea9322097987f7a595476fed86939f6e750c679df4753127de0e4f324bde` | `c47159bfa47777aa2866d5941d313a55b2f361d7bdd83b348e52f43a34a38df3` | `f84cea9322097987f7a595476fed86939f6e750c679df4753127de0e4f324bde` | 사망, 신규 분할 테스트 실패, exit 1 |
| S3 | `f84cea9322097987f7a595476fed86939f6e750c679df4753127de0e4f324bde` | `231f062f288d6866e729f060d2a89f6c2e969f5dac2ffa2e68d12ebff5e5f174` | `f84cea9322097987f7a595476fed86939f6e750c679df4753127de0e4f324bde` | 사망, 기존 3개 테스트 실패, 실제 라우터 위반 28건, exit 1 |
| S4 | `f84cea9322097987f7a595476fed86939f6e750c679df4753127de0e4f324bde` | `73ecd68c77b4a53592ce935b59ac79d724daae11367eee66606183ade1d29796` | `f84cea9322097987f7a595476fed86939f6e750c679df4753127de0e4f324bde` | 사망, 신규 분할 테스트 실패, exit 1 |

S3의 28건은 admin 5, auth 8, membership 2, notices 7, users 6이며 O104 주석 수치와 일치했다.

## 검증 결과

```text
DATABASE_URL=sqlite:///:memory: python -m pytest -q -p no:cacheprovider tests/test_api_rate_limit_docs.py tests/test_ai_analysis_endpoint.py
51 passed in 4.80s

DATABASE_URL=sqlite:///:memory: python -m pytest -q -m "slow or not slow" --collect-only
964 tests collected in 3.72s

DATABASE_URL=sqlite:///:memory: python -m pytest -q -m "slow or not slow"
964 passed, 5 warnings in 141.65s (0:02:21)
```

경고 5건은 `tests/test_gen_docs_index.py`의 임시 저장소 시나리오에서 `git ls-files` 실패 후 파일시스템 폴백을 알리는 기존 `UserWarning`이다. RuntimeWarning이나 실패는 없었다.

구현 커밋 CI는 SHA를 직접 대조했다.

```text
SHA=9a49b3d1b970f60510f6d667fdea086ed7e6ccdc
RID=35802416181
conclusion=success
```

## 서버 상태

- URL: 해당 없음 — 서버를 기동하지 않았다.
- health: 미호출 — packet이 서버 기동/재기동을 금지했다.
- PID: 해당 없음.
- 최신 코드 반영 여부: 실행 중 서버에 반영하지 않았으며 배포도 하지 않았다.

## 남은 리스크 / 팀 결정 필요 항목

- 구현상 남은 리스크나 팀 결정 필요 항목 없음.
- O124 종결 원장 반영은 Work Packet 허용 파일 목록 밖이므로 Claude 리뷰 단계에서 처리해야 한다.

## Claude 리뷰 요청

`docs/claude_handoff_phase182.md` 파일을 읽고 리뷰/검증해줘.
직접 수정하지 말고 Critical / Major / Minor 이슈와 개선 제안만 정리해줘.

특히 공유 술어가 두 가드에 실제로 단일 연결됐는지, S1~S4 변이 증거가 각 요구사항을 충분히 고정하는지 확인해 달라.
