import pandas as pd
import pytest

from scripts.build_forecast_dataset import make_window


def _raw_df(years=7):
    return pd.DataFrame([
        {
            "company": "A",
            "year": 2018 + i,
            "revenue": 1000 + i,
            "operating_profit": 100 + i,
            "net_income": 80 + i,
            "debt": 400 + i,
            "equity": 600 + i,
            "cash": 50 + i,
        }
        for i in range(years)
    ])


def test_make_window_raises_on_missing_columns():
    with pytest.raises(ValueError, match="필수 컬럼 누락"):
        make_window(pd.DataFrame({"company": ["A"], "year": [2020]}))


def test_make_window_skips_companies_under_window():
    with pytest.raises(ValueError, match="학습 샘플을 생성할 수 없습니다"):
        make_window(_raw_df(years=4))


def test_make_window_emits_correct_columns():
    result = make_window(_raw_df(years=7))

    expected = ["next_operating_profit"]
    for field in ("revenue", "op", "net", "debt", "equity", "cash"):
        expected.extend(f"{field}_{i}" for i in range(5))

    assert len(result) == 2
    assert set(expected).issubset(result.columns)
