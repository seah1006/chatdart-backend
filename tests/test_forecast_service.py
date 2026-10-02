from pathlib import Path

import pytest
from joblib import dump

from app.services import forecast_service
from app.services.feature_engineering import FEATURE_COLUMNS, FEATURE_COLUMNS_FIN


class _ColumnCheckingModel:
    def predict(self, x):
        assert list(x.columns) == FEATURE_COLUMNS
        return [123.0]


class _FinancialColumnCheckingModel:
    def predict(self, x):
        assert list(x.columns) == FEATURE_COLUMNS_FIN
        return [0.12]


def test_predict_passes_dataframe_with_feature_columns_order(tmp_path, monkeypatch):
    model_path = tmp_path / "model.pkl"
    dump(_ColumnCheckingModel(), model_path)
    monkeypatch.setenv("MODEL_PATH", str(model_path))
    # setattr 로 패치해야 teardown 에서 복원된다. 직접 대입하면 임시 경로가 모듈 전역에
    # 남아 app.main._AI_MODEL_PATH(import 시점 바인딩)까지 오염된다.
    monkeypatch.setattr(forecast_service, "MODEL_PATH", Path(model_path))
    monkeypatch.setattr(forecast_service, "_model", None)

    result = forecast_service.predict({key: 0.0 for key in FEATURE_COLUMNS})

    assert result == 123.0


def test_predict_raises_runtime_error_when_model_missing(tmp_path, monkeypatch):
    model_path = tmp_path / "missing.pkl"
    monkeypatch.setenv("MODEL_PATH", str(model_path))
    monkeypatch.setattr(forecast_service, "MODEL_PATH", Path(model_path))
    monkeypatch.setattr(forecast_service, "_model", None)

    with pytest.raises(RuntimeError, match="예측 모델 파일이 없습니다"):
        forecast_service.predict({})


def test_predict_op_profit_back_converts_margin_to_krw(monkeypatch):
    monkeypatch.setattr(forecast_service, "predict", lambda features: 0.1)

    result = forecast_service.predict_op_profit({"any": 0.0}, base_revenue=1_000_000_000)
    assert result == 100_000_000

    assert forecast_service.predict_op_profit({"any": 0.0}, base_revenue=0) == 0.0


def test_predict_net_income_roe_back_conversion(monkeypatch):
    monkeypatch.setattr(forecast_service, "_get_model_fin", lambda: _FinancialColumnCheckingModel())

    features = {key: 0.0 for key in FEATURE_COLUMNS_FIN}
    features["equity_growth"] = 0.25
    result = forecast_service.predict_net_income(
        features,
        base_equity=1_000_000_000,
    )

    assert result == 150_000_000


def test_predict_net_income_zero_equity_growth_matches_base_equity(monkeypatch):
    monkeypatch.setattr(forecast_service, "_get_model_fin", lambda: _FinancialColumnCheckingModel())

    result = forecast_service.predict_net_income(
        {key: 0.0 for key in FEATURE_COLUMNS_FIN},
        base_equity=1_000_000_000,
    )

    assert result == 120_000_000


def test_predict_net_income_raises_when_model_missing(tmp_path, monkeypatch):
    model_path = tmp_path / "missing_fin.pkl"
    monkeypatch.setenv("MODEL_PATH_FIN", str(model_path))
    monkeypatch.setattr(forecast_service, "MODEL_PATH_FIN", Path(model_path))
    monkeypatch.setattr(forecast_service, "_model_fin", None)

    with pytest.raises(RuntimeError, match="금융 예측 모델 파일이 없습니다"):
        forecast_service.predict_net_income({}, base_equity=1)
