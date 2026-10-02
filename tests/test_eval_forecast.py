import csv
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock

import numpy as np
import pytest

from app.db.models import FinancialStatement
from app.services import forecast_service
from app.services.dart_service import DartService
from scripts.collect_backtest import CSV_COLUMNS
from scripts import collect_backtest
from scripts import eval_forecast


def _row(year, financial=False, missing=None):
    row = {
        "stock_code": "000001", "company_name": "테스트", "fiscal_year": year,
        "is_financial": 1 if financial else 0, "revenue": 1000, "operating_profit": 100,
        "net_income": 80, "total_assets": 2000, "total_liabilities": 1000,
        "equity": 1000, "cash": 100, "net_interest_income": 50,
        "loan_loss_provision": 10, "insurance_liability": 20,
        "sector_detail": "bank" if financial else "", "source_report": "",
    }
    if missing:
        row[missing] = None
    return row


def _write_panel(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def test_window_requires_six_years_nonfin(monkeypatch, tmp_path, capsys):
    path = tmp_path / "panel.csv"
    _write_panel(path, [_row(year) for year in range(2020, 2025)])
    model_path = tmp_path / "model.pkl"
    model_path.touch()
    monkeypatch.setattr(forecast_service, "MODEL_PATH", model_path)
    assert eval_forecast.main(["--panel", str(path), "--model", "nonfin"]) == 0
    assert "비금융 샘플 0개" in capsys.readouterr().out


def test_window_three_years_yields_fin_sample(monkeypatch):
    monkeypatch.setattr(forecast_service, "predict_net_income", lambda features, equity: 90)
    samples, excluded, zero_filled, gapped = eval_forecast.build_samples(
        [_row(y, True) for y in range(2021, 2024)], "fin"
    )
    assert len(samples) == 1
    assert excluded == 0
    assert zero_filled == 0
    assert gapped == 0


def test_smape_matches_definition():
    metrics = eval_forecast.compute_metrics([{"actual": 100, "pred": 50, "prev": 80}])
    assert metrics["smape"] == pytest.approx(66.6667, rel=1e-4)


def test_direction_hit_excludes_flat_actual():
    metrics = eval_forecast.compute_metrics([
        {"actual": 100, "pred": 110, "prev": 100},
        {"actual": 120, "pred": 130, "prev": 100},
    ])
    assert metrics["direction_excluded"] == 1
    assert metrics["direction_hit"] == 100


def test_baseline_improvement_can_be_negative():
    metrics = eval_forecast.compute_metrics([{"actual": 100, "pred": 200, "prev": 90}])
    assert metrics["improvement"] < 0


def test_missing_value_sample_excluded(monkeypatch):
    rows = [_row(y) for y in range(2019, 2025)]
    rows[2]["revenue"] = None
    monkeypatch.setattr(forecast_service, "predict_op_profit", lambda features, revenue: 100)
    samples, excluded, zero_filled, gapped = eval_forecast.build_samples(rows, "nonfin")
    assert samples == []
    assert excluded == 1
    assert zero_filled == 0
    assert gapped == 0


def test_optional_field_zero_filled_not_excluded(monkeypatch):
    rows = [_row(y) for y in range(2019, 2025)]
    rows[2]["cash"] = None
    monkeypatch.setattr(forecast_service, "predict_op_profit", lambda features, revenue: 100)
    samples, excluded, zero_filled, gapped = eval_forecast.build_samples(rows, "nonfin")
    assert len(samples) == 1
    assert excluded == 0
    assert zero_filled == 1
    assert gapped == 0


def test_fin_optional_fields_all_null_still_sampled(monkeypatch):
    rows = [_row(y, True) for y in range(2021, 2024)]
    for row in rows:
        for field in ("net_interest_income", "loan_loss_provision", "insurance_liability"):
            row[field] = None
    monkeypatch.setattr(forecast_service, "predict_net_income", lambda features, equity: 90)
    samples, excluded, zero_filled, gapped = eval_forecast.build_samples(rows, "fin")
    assert len(samples) == 1
    assert excluded == 0
    assert zero_filled == 9
    assert gapped == 0


def test_target_missing_always_excluded(monkeypatch):
    rows = [_row(y) for y in range(2019, 2025)]
    rows[-1]["operating_profit"] = None
    monkeypatch.setattr(forecast_service, "predict_op_profit", lambda features, revenue: 100)
    samples, excluded, zero_filled, gapped = eval_forecast.build_samples(rows, "nonfin")
    assert samples == []
    assert excluded == 1
    assert zero_filled == 0
    assert gapped == 0


def test_report_shows_zero_fill_count():
    report = eval_forecast.build_report({"nonfin": ([], 3, 48, 0)})
    assert "보조 필드 결측(0 대체 대상): 48건" in report


def test_sector_detail_missing_is_counted(monkeypatch):
    rows = [_row(y, True) for y in range(2021, 2024)]
    for row in rows:
        row["sector_detail"] = ""
    monkeypatch.setattr(forecast_service, "predict_net_income", lambda features, equity: 90)
    samples, excluded, zero_filled, gapped = eval_forecast.build_samples(rows, "fin")
    report = eval_forecast.build_report({"fin": (samples, excluded, zero_filled, gapped)})
    assert "sector_detail 결측: 1개 샘플 / 전체 1개 (100.0%)" in report
    assert "운영 경로는 이 경우 종목코드로" in report


def test_sector_detail_present_no_warning(monkeypatch):
    monkeypatch.setattr(forecast_service, "predict_net_income", lambda features, equity: 90)
    samples, excluded, zero_filled, gapped = eval_forecast.build_samples(
        [_row(y, True) for y in range(2021, 2024)], "fin"
    )
    report = eval_forecast.build_report({"fin": (samples, excluded, zero_filled, gapped)})
    assert "sector_detail 결측: 0개 샘플 / 전체 1개 (0.0%)" in report
    assert "운영 경로는 이 경우 종목코드로" not in report


def test_eval_never_touches_dart_corp_list(monkeypatch):
    monkeypatch.setattr(DartService, "initialize", MagicMock(side_effect=AssertionError("DART init")))
    corp_list = MagicMock()
    corp_list.find_by_stock_code.side_effect = AssertionError("DART corp list access")
    monkeypatch.setattr(DartService, "_global_corp_list", corp_list)
    monkeypatch.setattr(forecast_service, "predict_net_income", lambda features, equity: 90)
    samples, _, _, _ = eval_forecast.build_samples(
        [_row(y, True) for y in range(2021, 2024)], "fin"
    )
    assert len(samples) == 1
    corp_list.find_by_stock_code.assert_not_called()


def test_nonfin_uses_predict_op_profit_conversion(monkeypatch):
    monkeypatch.setattr(forecast_service, "predict", lambda features: 0.2)
    samples, _, _, _ = eval_forecast.build_samples([_row(y) for y in range(2019, 2025)], "nonfin")
    assert samples[0]["pred"] == 200


def test_fin_uses_equity_growth_correction(monkeypatch):
    class Model:
        def predict(self, values):
            return np.array([0.1])
    monkeypatch.setattr(forecast_service, "_get_model_fin", lambda: Model())
    rows = [_row(2021, True), _row(2022, True), _row(2023, True)]
    rows[0]["equity"] = 800
    samples, _, _, _ = eval_forecast.build_samples(rows, "fin")
    assert samples[0]["pred"] == pytest.approx(125)


def test_missing_model_file_skips_gracefully(monkeypatch, tmp_path, capsys):
    path = tmp_path / "panel.csv"
    _write_panel(path, [_row(y, True) for y in range(2021, 2024)])
    missing = tmp_path / "missing.pkl"
    monkeypatch.setattr(forecast_service, "MODEL_PATH_FIN", missing)
    assert eval_forecast.main(["--panel", str(path), "--model", "fin"]) == 0
    assert "모델 파일 없음 — 건너뜀" in capsys.readouterr().out


def test_report_header_always_has_bias_warning():
    report = eval_forecast.build_report({})
    assert "학습 split 이 공개되지 않아" in report
    assert "낙관 편향" in report


def test_panel_roundtrip_collect_to_eval(test_engine, db_session, tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    for year in range(2021, 2024):
        db_session.add(FinancialStatement(
            company_id="000001", company_name="금융사", fiscal_year=year,
            revenue=100, operating_profit=10, net_income=8, total_assets=200,
            total_liabilities=100, equity=100, cash=20, net_interest_income=3,
            loan_loss_provision=1, insurance_liability=2, sector_detail="bank",
            source_report=f"금융사 {year}년 사업보고서 [금융업: bank]",
        ))
    db_session.commit()
    path = tmp_path / "panel.csv"
    collect_backtest.export_from_db(path, sessionmaker(bind=test_engine))
    monkeypatch.setattr(forecast_service, "predict_net_income", lambda features, equity: 9)
    samples, excluded, _, _ = eval_forecast.build_samples(eval_forecast.read_panel(path), "fin")
    assert len(samples) == 1
    assert excluded == 0


def test_eval_numeric_columns_subset_of_panel_columns():
    assert eval_forecast.NUMERIC_COLUMNS <= set(CSV_COLUMNS)


def test_year_gap_window_is_excluded(monkeypatch):
    monkeypatch.setattr(forecast_service, "predict_net_income", lambda features, equity: 90)
    samples, excluded, _, gapped = eval_forecast.build_samples(
        [_row(year, True) for year in (2016, 2017, 2024, 2025)], "fin"
    )
    assert samples == []
    assert excluded == 0
    assert gapped == 2


def test_target_year_must_follow_history(monkeypatch):
    monkeypatch.setattr(forecast_service, "predict_net_income", lambda features, equity: 90)
    samples, excluded, _, gapped = eval_forecast.build_samples(
        [_row(year, True) for year in (2021, 2022, 2024)], "fin"
    )
    assert samples == []
    assert excluded == 0
    assert gapped == 1


def test_consecutive_years_still_sampled(monkeypatch):
    monkeypatch.setattr(forecast_service, "predict_net_income", lambda features, equity: 90)
    samples, _, _, gapped = eval_forecast.build_samples(
        [_row(year, True) for year in (2021, 2022, 2023)], "fin"
    )
    assert len(samples) == 1
    assert gapped == 0


def test_report_shows_gap_count():
    report = eval_forecast.build_report({"fin": ([], 0, 0, 1)})
    assert "연도 불연속 제외: 1개" in report
    assert "연도가 끊긴 구간 1개" in report
    no_gap = eval_forecast.build_report({"fin": ([], 0, 0, 0)})
    assert "연도가 끊긴 구간" not in no_gap


def test_report_survives_cp949_stdout(tmp_path):
    path = tmp_path / "panel.csv"
    _write_panel(path, [_row(year) for year in range(2021, 2026)])
    result = subprocess.run(
        [sys.executable, "-m", "scripts.eval_forecast", "--panel", str(path)],
        capture_output=True,
        env={**os.environ, "PYTHONIOENCODING": "cp949"},
        cwd=Path(__file__).parents[1],
        timeout=120,
    )
    assert result.returncode == 0
    assert b"UnicodeEncodeError" not in result.stderr


def test_positive_table_hides_zero_fill_count():
    sample = {"actual": 100.0, "pred": 90.0, "prev": 80.0, "sector_missing": False}
    report = eval_forecast.build_report({"fin": ([sample], 0, 7, 0)})
    assert report.count("보조 필드 결측(0 대체 대상): 7건") == 1


def test_positive_table_hides_gap_count():
    sample = {"actual": 100.0, "pred": 90.0, "prev": 80.0, "sector_missing": False}
    report = eval_forecast.build_report({"fin": ([sample], 0, 0, 3)})
    assert report.count("연도 불연속 제외: 3개") == 1
    positive_section = report.split("### 흑자 구간", 1)[1]
    assert "연도 불연속 제외" not in positive_section


def test_sector_note_absent_when_no_missing():
    sample = {"actual": 100.0, "pred": 90.0, "prev": 80.0, "sector_missing": False}
    report = eval_forecast.build_report({"fin": ([sample], 0, 0, 0)})
    assert "sector_detail 결측: 0개 샘플" in report
    assert "업종 플래그" not in report


@pytest.mark.parametrize(
    ("excluded", "gapped", "has_window_hint"),
    [(0, 0, True), (1, 0, False), (0, 1, False)],
)
def test_nonfin_zero_sample_explains_window(
    monkeypatch, tmp_path, capsys, excluded, gapped, has_window_hint
):
    path = tmp_path / "panel.csv"
    _write_panel(path, [_row(year) for year in range(2021, 2026)])
    model_path = tmp_path / "model.pkl"
    model_path.touch()
    monkeypatch.setattr(forecast_service, "MODEL_PATH", model_path)
    monkeypatch.setattr(
        eval_forecast, "build_samples", lambda rows, model: ([], excluded, 0, gapped)
    )
    assert eval_forecast.main(["--panel", str(path), "--model", "nonfin"]) == 0
    assert ("6개년" in capsys.readouterr().out) is has_window_hint
