import csv
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy.orm import sessionmaker

from app.db.models import FinancialStatement
from app.services.dart_service import DartService
from scripts import collect_backtest


def _seed(db_session):
    for code, report in (("000001", "사업보고서"), ("000002", "[금융업: bank] 사업보고서")):
        for year in range(2021, 2024):
            db_session.add(FinancialStatement(
                company_id=code, company_name=f"회사{code}", fiscal_year=year,
                revenue=100, operating_profit=None if code == "000001" and year == 2021 else 10,
                net_income=8, total_assets=200, total_liabilities=100, equity=100, cash=20,
                net_interest_income=3 if code == "000002" else None,
                loan_loss_provision=1 if code == "000002" else None,
                insurance_liability=2 if code == "000002" else None,
                sector_detail="bank" if code == "000002" else None, source_report=report,
            ))
    db_session.commit()


def test_from_db_exports_expected_schema(test_engine, db_session, tmp_path):
    _seed(db_session)
    factory = sessionmaker(bind=test_engine)
    output = tmp_path / "panel.csv"
    assert collect_backtest.main(["--from-db", "--out", str(output)], factory) == 0
    with output.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert list(rows[0]) == collect_backtest.CSV_COLUMNS
    assert len(rows) == 6
    assert {row["stock_code"]: row["is_financial"] for row in rows} == {"000001": "0", "000002": "1"}


def test_from_db_keeps_none_as_empty(test_engine, db_session, tmp_path):
    _seed(db_session)
    output = tmp_path / "panel.csv"
    collect_backtest.export_from_db(output, sessionmaker(bind=test_engine))
    with output.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["net_interest_income"] == ""


def test_dry_run_makes_no_dart_call(monkeypatch, capsys):
    called = MagicMock(side_effect=AssertionError("DART call"))
    monkeypatch.setattr(DartService, "_collect_financial_data", called)
    monkeypatch.setattr(collect_backtest, "_stock_codes_from_db", lambda session_factory=None: ["005930"])
    monkeypatch.setattr(DartService, "_latest_fiscal_year", lambda: 2025)
    assert collect_backtest.main(["--from-year", "2018"]) == 0
    called.assert_not_called()
    assert "DART 실호출 0건" in capsys.readouterr().out


def _mock_live_path(monkeypatch, collected, corrected=None):
    corp = SimpleNamespace(corp_name="테스트", stock_code="005930")
    monkeypatch.setattr(DartService, "initialize", lambda: None)
    monkeypatch.setattr(DartService, "_global_corp_list", SimpleNamespace(find_by_stock_code=lambda _: corp))
    monkeypatch.setattr(DartService, "_is_financial_sector", lambda _: False)
    def fake_collect(corp, concepts, years, names):
        collected.append(list(years))
        return {year: {"fiscal_year": year} for year in years}
    monkeypatch.setattr(DartService, "_collect_financial_data", fake_collect)
    if corrected is None:
        monkeypatch.setattr(DartService, "_apply_corrections", lambda *args: [])
    else:
        monkeypatch.setattr(DartService, "_apply_corrections", corrected)


def test_live_splits_years_into_five_year_chunks(monkeypatch, tmp_path):
    seen = []
    _mock_live_path(monkeypatch, seen)
    collect_backtest.collect_live(["005930"], list(range(2016, 2026)), tmp_path / "out.csv")
    assert seen == [list(range(2021, 2026)), list(range(2016, 2021))]


def test_live_merges_chunks_before_corrections(monkeypatch, tmp_path):
    seen = []
    corrections = MagicMock(return_value=[])
    _mock_live_path(monkeypatch, seen, corrections)
    collect_backtest.collect_live(["005930"], list(range(2016, 2026)), tmp_path / "out.csv")
    corrections.assert_called_once()
    assert set(corrections.call_args.args[0]) == set(range(2016, 2026))


def test_eight_years_uses_two_chunks(monkeypatch, tmp_path):
    seen = []
    _mock_live_path(monkeypatch, seen)
    collect_backtest.collect_live(["005930"], list(range(2018, 2026)), tmp_path / "out.csv")
    assert len(seen) == 2


def test_five_years_or_less_uses_single_chunk(monkeypatch, tmp_path):
    seen = []
    _mock_live_path(monkeypatch, seen)
    collect_backtest.collect_live(["005930"], list(range(2021, 2026)), tmp_path / "out.csv")
    assert seen == [list(range(2021, 2026))]


@pytest.mark.parametrize("is_financial", [False, True])
def test_live_never_writes_db(monkeypatch, tmp_path, is_financial):
    from app.db import database

    seen = []
    _mock_live_path(monkeypatch, seen)
    monkeypatch.setattr(DartService, "_is_financial_sector", lambda _: is_financial)
    if is_financial:
        monkeypatch.setattr(DartService, "classify_financial_sector", lambda _: "bank")
    session = MagicMock(name="session")
    factory = MagicMock(name="SessionLocal", return_value=session)
    monkeypatch.setattr(database, "SessionLocal", factory)
    collect_backtest.collect_live(["005930"], list(range(2021, 2026)), tmp_path / "out.csv")
    factory.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()
    session.delete.assert_not_called()


def test_dry_run_plan_mentions_fallback_cost(capsys):
    collect_backtest._print_plan(49, list(range(2016, 2026)))
    output = capsys.readouterr().out
    assert "5년 청크 2개" in output
    assert "extract_fs 폴백" in output


def test_plan_output_survives_cp949_stdout():
    result = subprocess.run(
        [sys.executable, "-m", "scripts.collect_backtest", "--from-year", "2016"],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "cp949"},
        cwd=Path(__file__).parents[1],
        timeout=120,
    )
    assert result.returncode == 0
    assert b"UnicodeEncodeError" not in result.stderr
    assert b"dry-run" in result.stdout


def test_dry_run_survives_database_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(DartService, "_latest_fiscal_year", lambda: 2025)
    monkeypatch.setattr(
        collect_backtest, "_stock_codes_from_db",
        MagicMock(side_effect=RuntimeError("DB unavailable")),
    )
    assert collect_backtest.main(["--from-year", "2021"]) == 0
    assert "DB 미기동 — 종목 수 미확인" in capsys.readouterr().out


def test_from_db_warns_about_ignored_options(test_engine, db_session, tmp_path, capsys):
    output = tmp_path / "panel.csv"
    assert collect_backtest.main(
        ["--from-db", "--live", "--from-year", "2020", "--out", str(output)],
        sessionmaker(bind=test_engine),
    ) == 0
    assert "옵션은 무시합니다" in capsys.readouterr().out


def test_source_report_matches_operating_format():
    assert collect_backtest._source_report("테스트은행", 2025, True, "bank") == (
        "테스트은행 2025년 사업보고서 [금융업: bank]"
    )


def test_live_continues_after_chunk_failure(monkeypatch, tmp_path, capsys):
    seen = []
    corrections = MagicMock(return_value=[])
    _mock_live_path(monkeypatch, seen, corrections)

    def collect(_corp, _concepts, years, _names):
        if years[0] == 2016:
            raise RuntimeError("old chunk")
        return {year: {"fiscal_year": year} for year in years}

    monkeypatch.setattr(DartService, "_collect_financial_data", collect)
    collect_backtest.collect_live(["005930"], list(range(2016, 2026)), tmp_path / "out.csv")
    assert set(corrections.call_args.args[0]) == set(range(2021, 2026))
    assert "[경고] 005930 2016~2020 수집 실패" in capsys.readouterr().out


def test_live_skips_stock_when_all_chunks_fail(monkeypatch, tmp_path):
    corps = {code: SimpleNamespace(corp_name=code, stock_code=code) for code in ("000001", "000002")}
    monkeypatch.setattr(DartService, "initialize", lambda: None)
    monkeypatch.setattr(DartService, "_global_corp_list", SimpleNamespace(
        find_by_stock_code=lambda code: corps[code]
    ))
    monkeypatch.setattr(DartService, "_is_financial_sector", lambda _: False)
    monkeypatch.setattr(
        DartService, "_collect_financial_data",
        lambda corp, _concepts, years, _names: (
            (_ for _ in ()).throw(RuntimeError("failed")) if corp.stock_code == "000001"
            else {year: {"fiscal_year": year} for year in years}
        ),
    )
    corrections = MagicMock(return_value=[])
    monkeypatch.setattr(DartService, "_apply_corrections", corrections)
    collect_backtest.collect_live(list(corps), list(range(2021, 2026)), tmp_path / "out.csv")
    corrections.assert_called_once()
    assert corrections.call_args.args[2] == "000002"


def test_live_writes_panel_even_with_failures(monkeypatch, tmp_path):
    corps = {code: SimpleNamespace(corp_name=code, stock_code=code) for code in ("000001", "000002")}
    monkeypatch.setattr(DartService, "initialize", lambda: None)
    monkeypatch.setattr(DartService, "_global_corp_list", SimpleNamespace(
        find_by_stock_code=lambda code: corps[code]
    ))
    monkeypatch.setattr(DartService, "_is_financial_sector", lambda _: False)
    monkeypatch.setattr(
        DartService, "_collect_financial_data",
        lambda corp, _concepts, years, _names: (
            (_ for _ in ()).throw(RuntimeError("failed")) if corp.stock_code == "000001"
            else {year: {"fiscal_year": year} for year in years}
        ),
    )
    monkeypatch.setattr(DartService, "_apply_corrections", lambda data, name, code, report, fin: [
        {"company_id": code, "company_name": name, "fiscal_year": year,
         "source_report": report} for year in data
    ])
    output = tmp_path / "out.csv"
    collect_backtest.collect_live(list(corps), [2025], output)
    with output.open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert {row["stock_code"] for row in rows} == {"000002"}


def test_probe_splits_years_into_chunks(monkeypatch):
    seen = []
    _mock_live_path(monkeypatch, seen)
    collect_backtest.probe("005930", list(range(2014, 2026)))
    assert seen == [list(range(2021, 2026)), list(range(2016, 2021)), [2014, 2015]]


def test_probe_plan_matches_probe_execution(capsys):
    years = list(range(2014, 2026))
    collect_backtest._print_plan(1, years, "005930")
    assert f"5년 청크 {len(collect_backtest._year_chunks(years))}개" in capsys.readouterr().out


def test_from_db_failure_is_concise(tmp_path, capsys):
    factory = MagicMock(side_effect=RuntimeError("DB unavailable"))
    assert collect_backtest.main(["--from-db", "--out", str(tmp_path / "out.csv")], factory) == 1
    assert "[오류] DB 조회 실패: DB unavailable" in capsys.readouterr().out


def test_stocks_option_overrides_db_lookup(monkeypatch, capsys):
    lookup = MagicMock(side_effect=AssertionError("DB lookup"))
    monkeypatch.setattr(collect_backtest, "_stock_codes_from_db", lookup)
    monkeypatch.setattr(DartService, "_latest_fiscal_year", lambda: 2025)
    assert collect_backtest.main(["--stocks", "055550,086790", "--from-year", "2021"]) == 0
    lookup.assert_not_called()
    assert "대상 종목 2개" in capsys.readouterr().out


def test_stocks_option_filters_invalid_codes(monkeypatch, capsys):
    monkeypatch.setattr(DartService, "_latest_fiscal_year", lambda: 2025)
    assert collect_backtest.main(["--stocks", "055550,abc,086790"]) == 0
    output = capsys.readouterr().out
    assert "대상 종목 2개" in output
    assert "[경고] 무시: abc" in output


def test_stocks_option_works_without_db(monkeypatch):
    monkeypatch.setattr(DartService, "_latest_fiscal_year", lambda: 2025)
    monkeypatch.setattr(
        collect_backtest, "_stock_codes_from_db",
        MagicMock(side_effect=RuntimeError("DB unavailable")),
    )
    assert collect_backtest.main(["--stocks", "055550"]) == 0


def test_from_db_warns_about_stocks_option(test_engine, tmp_path, capsys):
    factory = sessionmaker(bind=test_engine)
    assert collect_backtest.main(
        ["--from-db", "--stocks", "055550", "--out", str(tmp_path / "out.csv")], factory
    ) == 0
    assert "--stocks 옵션은 무시합니다" in capsys.readouterr().out
