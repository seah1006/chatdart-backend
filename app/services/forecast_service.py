"""LightGBM next operating profit forecast service."""
import os
from pathlib import Path
from threading import Lock

import pandas as pd
from joblib import load

from app.services.feature_engineering import FEATURE_COLUMNS, FEATURE_COLUMNS_FIN


MODEL_PATH = Path(os.getenv("MODEL_PATH", "models/model_nonfin.pkl"))
MODEL_PATH_FIN = Path(os.getenv("MODEL_PATH_FIN", "models/model_fin.pkl"))

_model = None
_model_lock = Lock()
_model_fin = None
_model_fin_lock = Lock()


def _get_model():
    """Lazy load model file."""
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        if not MODEL_PATH.exists():
            raise RuntimeError(
                f"예측 모델 파일이 없습니다: {MODEL_PATH}. "
                "scripts/train_forecast.py 로 학습 후 배치하세요."
            )
        _model = load(MODEL_PATH)
        return _model


def _get_model_fin():
    """Lazy load financial model file."""
    global _model_fin
    if _model_fin is not None:
        return _model_fin
    with _model_fin_lock:
        if _model_fin is not None:
            return _model_fin
        if not MODEL_PATH_FIN.exists():
            raise RuntimeError(
                f"금융 예측 모델 파일이 없습니다: {MODEL_PATH_FIN}. "
                "AI 파트가 제공한 models/model_fin.pkl 을 배치하세요."
            )
        _model_fin = load(MODEL_PATH_FIN)
        return _model_fin


def predict(data):
    """Return model raw output (operating profit margin, 0~1).

    Notes:
        - Non-financial model (`models/model_nonfin.pkl`) predicts the next operating profit margin,
          not KRW operating profit.
        - For KRW operating profit, use `predict_op_profit()`.
    """
    model = _get_model()
    x = pd.DataFrame([data], columns=FEATURE_COLUMNS)
    return float(model.predict(x)[0])


def predict_op_profit(features: dict, base_revenue: float) -> float:
    """Convert predicted margin into KRW operating profit using latest revenue.

    Args:
        features: Feature dict for the LightGBM model (32 columns).
        base_revenue: Latest revenue (e.g., `revenue_4`) used as the conversion base.

    Returns:
        Predicted next operating profit in KRW.
    """
    pred_margin = predict(features)
    return pred_margin * float(base_revenue or 0.0)


def predict_net_income(features: dict, base_equity: float) -> float:
    """Predict financial-sector ROE and convert it into KRW net income.

    The model target is next-year ROE. Convert with growth-adjusted equity:
    estimated_next_equity = equity_1 * (1 + equity_growth).
    """
    model = _get_model_fin()
    x = pd.DataFrame([features], columns=FEATURE_COLUMNS_FIN)
    pred_roe = float(model.predict(x)[0])
    equity_growth = float(features.get("equity_growth", 0.0))
    estimated_next_equity = float(base_equity or 0.0) * (1.0 + equity_growth)
    return pred_roe * estimated_next_equity
