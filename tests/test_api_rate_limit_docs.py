"""Swagger rate-limit descriptions must match endpoint decorators."""
import ast
import re
from pathlib import Path

import pytest
from limits import RateLimitItemPerMinute, parse


API_DIR = Path(__file__).resolve().parents[1] / "app" / "api" / "v1"
_DOC_RATE_RE = re.compile(r"분당\s*(\d+)회")


def _is_finance_router(filename):
    return filename.startswith("finance") and filename.endswith(".py")


# finance.py의 429 설명을 가진 24개 엔드포인트를 명시한다.
# finance.py의 limit 엔드포인트 전체가 이 집합과 정확히 일치해야 한다.
# 다른 5개 라우터의 28곳(auth 8·notices 7·users 6·admin 5·membership 2)은
# O104에 따라 시연(2026-09-30) 이후 처리할 때까지 의도적으로 면제한다.
# O104 진행 시 다음 3곳을 함께 바꾼다:
# ① _is_finance_router를 넓힌다(두 가드가 함께 바뀐다).
# ② test_non_finance_router_keeps_missing_429_exemption을 삭제하거나 반전한다.
# ③ test_documented_endpoint_set_ignores_other_routers를 삭제한다
#    (non-finance 항목이 _REQUIRES_429에 들어오면 이 테스트의 의미가 사라진다).
# finance.py를 finance_*.py로 나누면 새 파일도 두 가드의 범위에 들어간다.
# 옮긴 엔드포인트의 _REQUIRES_429 튜플 첫 원소를 새 파일명으로 바꿔야 한다.
# 현재는 admin.py에 429 설명을 추가했다가 나중에 조용히 지워도 어떤 가드도 잡지 못한다.
# 다만 현재 non-finance의 429 설명은 0개라 이 부수 효과는 무해하다.
_REQUIRES_429 = {
    ("finance.py", "search_company"),
    ("finance.py", "check_status"),
    ("finance.py", "check_status_batch"),
    ("finance.py", "refresh_collect"),
    ("finance.py", "collect"),
    ("finance.py", "get_ai_analysis"),
    ("finance.py", "get_summary_detail"),
    ("finance.py", "get_summary_trend"),
    ("finance.py", "get_similar_companies"),
    ("finance.py", "get_recommendation"),
    ("finance.py", "get_summary"),
    ("finance.py", "export_summary"),
    ("finance.py", "list_companies"),
    ("finance.py", "compare_companies"),
    ("finance.py", "compare_ai_insight"),
    ("finance.py", "export_compare"),
    ("finance.py", "save_compare"),
    ("finance.py", "get_compare_share"),
    ("finance.py", "delete_company"),
    ("finance.py", "collect_batch"),
    ("finance.py", "get_popular"),
    ("finance.py", "get_search_popular"),
    ("finance.py", "interpret_financial_insights"),
    ("finance.py", "get_signal_disclosures"),
}


def _assignments(tree):
    result = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            result[node.target.id] = node.value
    return result


def _response_entries(expression, assignments, seen=None):
    seen = set() if seen is None else seen
    if isinstance(expression, ast.Name):
        if expression.id in seen or expression.id not in assignments:
            return {}
        return _response_entries(assignments[expression.id], assignments, seen | {expression.id})
    if not isinstance(expression, ast.Dict):
        return {}

    entries = {}
    for key, value in zip(expression.keys, expression.values):
        if key is None:
            entries.update(_response_entries(value, assignments, seen))
        elif isinstance(key, ast.Constant) and key.value == 429:
            entries[429] = value
    return entries


def _constant_string(expression):
    return expression.value if isinstance(expression, ast.Constant) and isinstance(expression.value, str) else None


def _response_description(expression):
    if not isinstance(expression, ast.Dict):
        return None
    for key, value in zip(expression.keys, expression.values):
        if isinstance(key, ast.Constant) and key.value == "description":
            return _constant_string(value)
    return None


def _endpoint_rate_and_responses(function, assignments):
    rates = []
    responses = None
    for decorator in function.decorator_list:
        if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
            continue
        if isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == "limiter" and decorator.func.attr == "limit":
            rates.append(_constant_string(decorator.args[0]) if decorator.args else None)
        for keyword in decorator.keywords:
            if keyword.arg == "responses":
                responses = _response_entries(keyword.value, assignments)
    return rates, responses


def _violations_for_source(source, filename):
    tree = ast.parse(source, filename=filename)
    assignments = _assignments(tree)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        rates, responses = _endpoint_rate_and_responses(node, assignments)
        if not rates:
            continue
        description = _response_description((responses or {}).get(429))
        context = f"{filename}:{node.name} decorator={rates!r} docs={description!r}"
        if len(rates) > 1:
            violations.append(f"{context}: multiple limit decorators")
            continue
        rate = rates[0]
        try:
            if not isinstance(rate, str):
                raise ValueError("limit is not a constant string")
            parsed = parse(rate)
        except (ValueError, TypeError) as exc:
            violations.append(f"{context}: invalid rate limit ({exc})")
            continue
        if not description:
            # _is_finance_router 밖의 누락은 O104에 따라 시연(2026-09-30) 이후 처리한다.
            if _is_finance_router(filename):
                violations.append(f"{context}: required 429 description missing")
            continue
        if not isinstance(parsed, RateLimitItemPerMinute) or parsed.multiples != 1:
            if "분당" in description:
                violations.append(f"{context}: non-minute rate with minute docs")
            continue
        documented = _DOC_RATE_RE.search(description)
        if documented is None or int(documented.group(1)) != parsed.amount:
            violations.append(f"{context}: rate/document mismatch")
    return violations


def test_rate_limit_decorators_match_swagger_429_descriptions():
    mismatches = []
    for source_path in sorted(API_DIR.glob("*.py")):
        mismatches.extend(_violations_for_source(
            source_path.read_text(encoding="utf-8"), source_path.name
        ))

    assert not mismatches, "Swagger rate-limit mismatch: " + "; ".join(mismatches)


def test_guard_rejects_multiple_limit_decorators():
    source = '''
@router.get("/sample", responses={429: {"description": "분당 20회"}})
@limiter.limit("1/minute")
@limiter.limit("20/minute")
def sample():
    pass
'''
    violations = _violations_for_source(source, "synthetic.py")
    assert len(violations) == 1
    assert "synthetic.py:sample" in violations[0]
    assert "multiple limit decorators" in violations[0]
    assert "1/minute" in violations[0] and "20/minute" in violations[0]


def test_guard_rejects_non_minute_rate_with_minute_docs():
    source = '''
@router.get("/sample", responses={429: {"description": "분당 3회"}})
@limiter.limit("3 per hour")
def sample():
    pass
'''
    violations = _violations_for_source(source, "synthetic.py")
    assert len(violations) == 1
    assert "synthetic.py:sample" in violations[0]
    assert "non-minute rate with minute docs" in violations[0]
    assert "3 per hour" in violations[0] and "분당 3회" in violations[0]


def test_finance_guard_rejects_unlisted_missing_429_description():
    source = '''
@router.get("/sample")
@limiter.limit("20/minute")
def new_finance_endpoint():
    pass
'''
    violations = _violations_for_source(source, "finance.py")
    assert len(violations) == 1
    assert "required 429 description missing" in violations[0]


def test_non_finance_router_keeps_missing_429_exemption():
    source = '''
@router.get("/sample")
@limiter.limit("20/minute")
def new_admin_endpoint():
    pass
'''
    assert _violations_for_source(source, "admin.py") == []


def test_finance_split_file_is_in_scope_for_both_guards(tmp_path):
    (tmp_path / "finance.py").write_text('''
@router.get("/kept", responses={429: {"description": "분당 20회"}})
@limiter.limit("20/minute")
def kept_documented():
    pass
''', encoding="utf-8")
    compare_source = '''
@router.get("/moved", responses={429: {"description": "분당 20회"}})
@limiter.limit("20/minute")
def moved_documented():
    pass

@router.get("/missing")
@limiter.limit("20/minute")
def moved_undocumented():
    pass
'''
    (tmp_path / "finance_compare.py").write_text(compare_source, encoding="utf-8")

    violations = _violations_for_source(compare_source, "finance_compare.py")
    assert len(violations) == 1
    assert "required 429 description missing" in violations[0]
    assert "moved_undocumented" in violations[0]
    assert _documented_endpoints(tmp_path) == {
        ("finance.py", "kept_documented"),
        ("finance_compare.py", "moved_documented"),
    }


def test_documented_endpoint_set_ignores_other_routers(tmp_path):
    (tmp_path / "finance.py").write_text('''
@router.get("/documented", responses={429: {"description": "분당 20회"}})
@limiter.limit("20/minute")
def documented_finance_endpoint():
    pass

@router.get("/undocumented")
@limiter.limit("20/minute")
def undocumented_finance_endpoint():
    pass
''', encoding="utf-8")
    (tmp_path / "admin.py").write_text('''
@router.get("/sample", responses={429: {"description": "분당 20회"}})
@limiter.limit("20/minute")
def newly_documented_admin():
    pass
''', encoding="utf-8")

    documented = _documented_endpoints(tmp_path)
    assert documented == {("finance.py", "documented_finance_endpoint")}


def test_guard_rejects_unparsable_rate_limit():
    source = '''
@router.get("/sample", responses={429: {"description": "분당 20회"}})
@limiter.limit("not-a-rate")
def sample():
    pass
'''

    violations = _violations_for_source(source, "synthetic.py")
    assert len(violations) == 1
    assert "invalid rate limit" in violations[0]


def test_guard_scans_finance_router():
    paths = sorted(API_DIR.glob("*.py"))
    assert "finance.py" in {path.name for path in paths}
    source = (API_DIR / "finance.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="finance.py")
    assignments = _assignments(tree)
    limited = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _endpoint_rate_and_responses(node, assignments)[0]
    ]
    assert len(limited) >= 20


def _documented_endpoints(api_dir):
    documented = set()
    paths = [p for p in sorted(api_dir.glob("*.py")) if _is_finance_router(p.name)]
    assert paths, f"finance 라우터 파일을 찾지 못했다: {api_dir}"
    for source_path in paths:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=source_path.name)
        assignments = _assignments(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            rates, responses = _endpoint_rate_and_responses(node, assignments)
            if rates and _response_description((responses or {}).get(429)):
                documented.add((source_path.name, node.name))
    return documented


def test_requires_429_matches_documented_endpoints():
    _assert_documented_endpoints(_documented_endpoints(API_DIR))


def _assert_documented_endpoints(documented):
    added = sorted(documented - _REQUIRES_429)
    removed = sorted(_REQUIRES_429 - documented)
    message = (
        "finance.py의 429 설명 집합이 실제 엔드포인트와 일치해야 한다. "
        "finance.py에 429 설명을 새로 추가했다면 _REQUIRES_429 집합에도 등록하라. "
        f"추가된 항목(집합에 등록 필요)={added}; 사라진 항목(설명이 삭제됨)={removed}"
    )
    assert documented == _REQUIRES_429, message


def test_requires_429_failure_message_names_the_gap():
    documented = {("finance.py", "search_company"), ("finance.py", "missing_endpoint")}
    with pytest.raises(AssertionError) as exc_info:
        _assert_documented_endpoints(documented)
    assert str(exc_info.value).startswith("finance.py의 429 설명 집합이")
    assert "_REQUIRES_429 집합에도 등록하라" in str(exc_info.value)
    assert "missing_endpoint" in str(exc_info.value)

