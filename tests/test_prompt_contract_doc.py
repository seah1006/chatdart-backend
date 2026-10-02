from pathlib import Path
import re
import sys

import pytest

from app.services.ai_summary_service import INSIGHT_PROMPT_VERSION


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse_contract_test_cells(contract):
    loose_rows = re.findall(r"^\| R\d{2} \|.*$", contract, re.MULTILINE)
    pattern = r"^\| R\d{2} \| [^|]* \| [^|]* \| ([^|]*) \| [^|]* \|$"
    parsed = [re.fullmatch(pattern, row) for row in loose_rows]
    unparsed = [row[:160] for row, match in zip(loose_rows, parsed) if match is None]
    assert not unparsed, f"파싱되지 않은 계약 행 ({len(unparsed)}/{len(loose_rows)}): {unparsed}"
    return [match.group(1) for match in parsed if match is not None]


def test_prompt_rule_count_matches_contract_table():
    service_source = (
        PROJECT_ROOT / "app" / "services" / "ai_summary_service.py"
    ).read_text(encoding="utf-8")
    rules_block = service_source.split("반드시 지킬 규칙:\n", 1)[1].split(
        "\n\n다음 키를 가진 JSON 객체만 출력하라:", 1
    )[0]
    prompt_rule_lines = [
        line for line in rules_block.splitlines() if line.startswith("- ")
    ]

    contract = (PROJECT_ROOT / "docs" / "prompt_contract.md").read_text(
        encoding="utf-8"
    )
    contract_rows = re.findall(r"^\| R\d{2} \|", contract, flags=re.MULTILINE)

    assert prompt_rule_lines, "interpret_insights 프롬프트 규칙을 찾지 못했습니다."
    assert len(contract_rows) == len(prompt_rule_lines)


def test_prompt_rule_bodies_match_contract_rows_in_order():
    service_source = (
        PROJECT_ROOT / "app" / "services" / "ai_summary_service.py"
    ).read_text(encoding="utf-8")
    rules_block = service_source.split("반드시 지킬 규칙:\n", 1)[1].split(
        "\n\n다음 키를 가진 JSON 객체만 출력하라:", 1
    )[0]
    prompt_rules = [
        line[2:] for line in rules_block.splitlines() if line.startswith("- ")
    ]

    contract = (PROJECT_ROOT / "docs" / "prompt_contract.md").read_text(
        encoding="utf-8"
    )
    contract_rules = []
    for row in re.findall(r"^\| R\d{2} \|.*$", contract, re.MULTILINE):
        fields = re.split(r"(?<!\\)\|", row.strip())[1:-1]
        assert len(fields) == 5, f"계약 행 열 수 불일치: {row[:160]}"
        contract_rules.append((fields[0].strip(), fields[1].strip().replace(r"\|", "|")))

    assert len(prompt_rules) == len(contract_rules)
    for index, (rule_id, contract_body) in enumerate(contract_rules):
        assert prompt_rules[index] == contract_body, (
            f"프롬프트·계약 본문 불일치: {rule_id} (순번 {index + 1})"
        )


def test_prompt_contract_references_existing_test_functions():
    contract = (PROJECT_ROOT / "docs" / "prompt_contract.md").read_text(
        encoding="utf-8"
    )
    contract_rows = _parse_contract_test_cells(contract)
    referenced_tests = [
        test_name
        for tests_cell in contract_rows
        for test_name in re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", tests_cell)
    ]

    assert referenced_tests, "프롬프트 계약에서 고정 테스트 함수명을 찾지 못했습니다."

    test_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (PROJECT_ROOT / "tests").rglob("test_*.py")
    )
    existing_tests = set(re.findall(r"^\s*(?:async )?def ([A-Za-z_][A-Za-z0-9_]*)\(", test_sources, re.MULTILINE))
    missing_tests = sorted(set(referenced_tests) - existing_tests)

    assert not missing_tests, f"존재하지 않는 고정 테스트 함수: {missing_tests}"


def test_prompt_contract_version_matches_code_constant():
    contract = (PROJECT_ROOT / "docs" / "prompt_contract.md").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r"프롬프트 버전:\s*`?INSIGHT_PROMPT_VERSION\s*=\s*(\d+)`?", contract
    )
    assert match is not None, "프롬프트 계약 문서에서 버전 표기를 찾지 못했습니다."
    assert int(match.group(1)) == INSIGHT_PROMPT_VERSION


def test_prompt_contract_documents_primary_flag_selection_rules():
    contract = (PROJECT_ROOT / "docs" / "prompt_contract.md").read_text(encoding="utf-8")
    assert "## primary_flag 선택 규칙\n" in contract, "primary_flag 선택 규칙 절이 없습니다."
    section = contract.split("## primary_flag 선택 규칙\n", 1)[1]
    section = re.split(r"\n## ", section, maxsplit=1)[0]
    assert re.findall(r"^\d+\. ", section, re.MULTILINE) == [f"{i}. " for i in range(1, 8)]
    assert "interpret_insights" in section
    assert "select_primary_flag" in section
    assert "warning_flags + positive_flags" in section
    assert "`flag.year`가 가장 최신인" in section
    assert "warning이 positive보다 우선" in section
    assert "primary_flag: 없음" in section
    assert "연도 없이 회사명으로 시작" in section
    assert "도달하지 않는다" in section
    assert "AI 계약에 명시돼 있지 않다" in section
    assert "모델이 아니라 백엔드" in section
    assert "B6 A안" in section
    assert "AI 결정 대기" not in contract
    assert "O66" in section
    assert "AI 결정에 맡기지 않는다" not in contract
    headline_row = next(row for row in contract.splitlines() if row.startswith("| `headline` |"))
    assert "primary_flag" in headline_row
    assert "latest_year" not in headline_row
    assert "신호 내용만 요약" in headline_row


def test_contract_guard_rejects_silently_skipped_escaped_pipe_row(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "docs" / "prompt_contract.md").write_text(
        "| R01 | normal | tier 1 | `test_present` | 1 |\n"
        r"| R02 | escaped \| pipe | tier 1 | `test_missing` | 1 |" + "\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_insights_interpret.py").write_text("def test_present(): pass\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "PROJECT_ROOT", tmp_path)
    with pytest.raises(AssertionError, match="파싱되지 않은 계약 행") as exc:
        test_prompt_contract_references_existing_test_functions()
    assert "R02" in str(exc.value)
    assert "escaped" in str(exc.value)


def test_contract_guard_finds_relocated_test_in_other_file(tmp_path, monkeypatch):
    (tmp_path / "docs").mkdir()
    tests = tmp_path / "tests"
    (tests / "nested").mkdir(parents=True)
    (tmp_path / "docs" / "prompt_contract.md").write_text(
        "| R01 | normal | tier 1 | `test_relocated` | 1 |\n", encoding="utf-8",
    )
    (tests / "test_insights_interpret.py").write_text("", encoding="utf-8")
    (tests / "nested" / "test_other.py").write_text("def test_relocated(): pass\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "PROJECT_ROOT", tmp_path)
    test_prompt_contract_references_existing_test_functions()
