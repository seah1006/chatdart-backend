"""LightGBM training pipeline synced with AI server for Phase 48."""
import logging
import os
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from joblib import dump
from sklearn.metrics import mean_absolute_error, mean_absolute_percentage_error
from sklearn.model_selection import TimeSeriesSplit

ROOT_DIR = Path(__file__).resolve().parents[1]
import sys

if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.feature_engineering import create_features_df  # noqa: E402
from scripts.build_forecast_dataset import make_window  # noqa: E402


warnings.filterwarnings("ignore", category=UserWarning)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DATA_PATH = "data/train.csv"
MODEL_DIR = "models"
MODEL_PATH = os.path.join(MODEL_DIR, "model.pkl")

LGBM_PARAMS = dict(
    n_estimators=2000,
    learning_rate=0.01,
    max_depth=4,
    num_leaves=15,
    min_child_samples=5,
    subsample=0.8,
    subsample_freq=1,
    colsample_bytree=0.8,
    reg_alpha=0.1,
    reg_lambda=1.0,
    random_state=42,
    n_jobs=-1,
    verbose=-1,
)
EARLY_STOPPING_ROUNDS = 50
N_CV_SPLITS = 5


def run_training(raw_df: pd.DataFrame | None = None) -> dict:
    """Run training and save the final model."""
    from lightgbm import LGBMRegressor, early_stopping, log_evaluation

    if raw_df is None:
        log.info("학습 데이터 로드: %s", DATA_PATH)
        raw_df = pd.read_csv(DATA_PATH)
    log.info("원시 데이터 shape: %s", raw_df.shape)

    window_df = make_window(raw_df)
    log.info("윈도우 샘플 수: %d", len(window_df))

    x = create_features_df(window_df)
    y = window_df["next_operating_profit"].values
    log.info("피처 수: %d", x.shape[1])

    tscv = TimeSeriesSplit(n_splits=N_CV_SPLITS)
    cv_mae = []
    cv_mape = []
    best_iterations = []

    for fold, (train_idx, val_idx) in enumerate(tscv.split(x), 1):
        x_train, x_val = x.iloc[train_idx], x.iloc[val_idx]
        y_train, y_val = y[train_idx], y[val_idx]
        model_cv = LGBMRegressor(**LGBM_PARAMS)
        model_cv.fit(
            x_train,
            y_train,
            eval_set=[(x_val, y_val)],
            callbacks=[
                early_stopping(EARLY_STOPPING_ROUNDS, verbose=False),
                log_evaluation(period=-1),
            ],
        )
        preds = model_cv.predict(x_val)
        fold_mae = mean_absolute_error(y_val, preds)
        fold_mape = mean_absolute_percentage_error(y_val, preds)
        cv_mae.append(fold_mae)
        cv_mape.append(fold_mape)
        best_iterations.append(getattr(model_cv, "best_iteration_", LGBM_PARAMS["n_estimators"]))
        log.info("Fold %d | MAE=%.2f | MAPE=%.2f%%", fold, fold_mae, fold_mape * 100)

    best_iter = max(int(np.mean(best_iterations)), 100)
    final_model = LGBMRegressor(**{**LGBM_PARAMS, "n_estimators": best_iter})
    final_model.fit(x, y)

    os.makedirs(MODEL_DIR, exist_ok=True)
    dump(final_model, MODEL_PATH)

    result = {
        "mae": round(float(np.mean(cv_mae)), 2),
        "mape": round(float(np.mean(cv_mape)) * 100, 2),
        "n_samples": int(len(x)),
        "model_path": MODEL_PATH,
        "best_iter": best_iter,
    }
    log.info("학습 결과: %s", result)
    return result


if __name__ == "__main__":
    run_training()
