# Phase 182 독립 리뷰 — 2026-09-23

기존 승인 영향: 없음 (리뷰 문서. 계약·스키마·프롬프트 무변경)

| 항목 | 값 |
|---|---|
| 대상 | `da7825a..1e06d5f` (구현 `9a49b3d`, handoff `1e06d5f`) |
| 방식 | `/code-review da7825a..1e06d5f high` (독립 lane) + Claude 직접 재현 |
| 판정 | **통과 (APPROVE) — Critical 0 / Major 0 / Minor 0** |
| `app/` 변경 | **없음** — `git diff --stat da7825a HEAD -- app/` 0줄 |
| CI | `9a49b3d` run **35802416181** success · `1e06d5f` run **35802835486** success — 둘 다 HEAD SHA 대조 |
| 후속 | 다음 packet 없음 (4절) |

---

## 1. 결론

**packet 182 작업 1~3 이 사양대로 구현됐고 O124 가 전건 해소됐다.**

- **작업 1** — `_is_finance_router(filename)` 한 곳에서 범위를 판정하고
  `_violations_for_source` 의 누락 검사와 `_documented_endpoints` 의 파일 선택이 **둘 다 이 술어를 호출한다.**
  두 함수에 `"finance"` 리터럴이 남지 않았다(handoff AST 검사). 신규 `test_finance_split_file_is_in_scope_for_both_guards`
  가 분할 파일의 **누락 검출(단언 ①)과 수집(단언 ②)을 한 테스트에서** 고정한다.
- **작업 2** — O104 이행 주석이 4곳 → 3곳(①이 공유 술어 하나로 합쳐짐)으로 갱신됐고 분할 시나리오 2줄이 들어갔다.
  식별자 이름이 코드와 일치한다.
- **작업 3** — `test_ai_analysis_returns_404_when_no_records` 만 삭제됐고 인접 테스트는 무변경. 고아 import 없음.

packet 작성 규칙 두 건(범위를 숫자로 제한하지 않음 · 단언 약화 변이 금지)을 적용한 첫 packet 이다.
**Codex 가 제약 충돌을 보고하지 않았고, 변이 S1~S4 가 전부 사망했다** — 두 규칙 모두 이번에는 결함을 만들지 않았다.

---

## 2. 독립 리뷰 결과

`/code-review` 는 **지적 0건**을 반환했다(`ReportFindings` 미가용이라 반환 본문이 유일한 기록 — 요지를 옮긴다).

- 삭제 테스트는 `tests/test_finance_endpoints.py:163-182` 파라미터 테스트 `[ai-analysis]` 케이스에 **완전 포섭** —
  같은 엔드포인트 · 같은 `get_resolved_code` 패치 · 같은 픽스처 · 레코드 0개이고, 그쪽은 404 **+ 정확 문구**까지 단언한다.
- 공유 술어가 두 가드에 연결돼 범위가 대칭이다. 현재 `app/api/v1/` 는 `finance.py` + 다른 라우터 5개 + `__init__.py` 라
  **스캔 대상 파일 집합은 변경 전과 같다.**
- `test_documented_endpoint_set_ignores_other_routers` 는 `admin.py` 가 술어에 걸리지 않으므로 계속 성립한다.
- **관찰(지적 아님)**: `finance_utils.py` 같은 비라우터 모듈이 생기면 술어에 걸린다. 그런 파일은 없고,
  `@limiter.limit` 이 없는 파일은 두 가드 어디에도 기여하지 않으므로 영향이 없다.

---

## 3. Claude 직접 재현

| 항목 | 명령 | 결과 |
|---|---|---|
| 집중 테스트 | `pytest -q tests/test_api_rate_limit_docs.py tests/test_ai_analysis_endpoint.py` | **51 passed** — handoff 와 일치 |
| 수집 개수 | `pytest -q -m "slow or not slow" --collect-only` | **964** — 산술 964 + 1 − 1 = 964, 문서 3종 불변 정당 |
| `app/` | `git diff --stat da7825a HEAD -- app/` | **0줄** |
| 변이 S1 재현 | `_violations_for_source` 의 `if _is_finance_router(filename):` → `if filename == "finance.py":` | **사망** — `FAILED ...::test_finance_split_file_is_in_scope_for_both_guards`, `1 failed, 10 passed` |
| S1 변이 전 해시 | `sha256sum` (작업 트리) | `f84cea93…` — **handoff 의 before 값과 일치** |
| CI | `gh run list` 에서 SHA 대조 | `9a49b3d` → 35802416181 success · `1e06d5f` → 35802835486 success |

### 해시 재현 시 주의 (지적 아님)

S1 원복을 `git checkout -- <file>` 로 했더니 해시가 `f84cea93…` → `1ef51a68…` 로 바뀌었다.
이 저장소는 `core.autocrlf=true` 라 **작업 트리 바이트가 커밋 blob 과 다를 수 있고**, checkout 은 blob 바이트(LF)로 다시 쓴다.
원복 전후 모두 `git status` clean 이고 내용은 HEAD 와 같다. handoff 의 before/restored 값은 **Codex 작업 트리 기준**이며
그 before 값이 재현됐으므로 증거로 유효하다.
**앞으로 변이 원복은 파일 복사본으로 되돌리거나, 해시를 비교할 때 이 차이를 감안한다.**

---

## 4. 후속 처리 — 다음 packet 을 쓰지 않는다

「handoff 리뷰가 끝나면 다음 packet 을 먼저 작성한다」는 규칙이 있지만 **이번에는 packet 을 쓰지 않는다.**

- 이번 리뷰의 지적이 **0건**이라 묶어 넘길 항목이 없다.
- **시연(2026-09-30) 전에 필요한 백엔드 코드 작업이 없다.** 운영 배포본은 Phase 174+176+177+178(`725ee29`)이다.
  Phase 180~182 는 테스트·문서 전용이다. **Phase 179 는 예외** — `219f993` 이 `app/services/ai_summary_service.py`
  의 죽은 코드 14줄을 삭제했다(런타임 불변, Phase 179 리뷰가 「배포 대상 아님」 판정). 따라서 운영 drift 점검은
  **이 파일 1개를 drift 로 보고한다** — `docs/demo_readiness_checklist_20260923.md` §1 참조.
  (2026-09-23 정정: 초판은 「Phase 179~182 는 테스트·문서 전용」이라 적었다.)
- 남은 시연 전 점검(배포 drift 확인)은 `scripts/deploy_aws.py` 가 SSH 를 쓰므로 **팀장 승인 사항**이지 packet 대상이 아니다.
- 억지로 packet 을 만들면 「요청한 것만 만든다」에 어긋난다. Phase 181 리뷰가 같은 판단을 내린 전례를 따른다.

원장: **O124 종결**(Phase 182). 같은 날 등록한 **O125**(AI 모델 교체 검토)는 성격상 「보류」 절로 옮겼다.

---

## 5. 배포 판단

**배포와 무관하다.** `app/` 변경이 없다.
