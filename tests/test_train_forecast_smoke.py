import pytest


@pytest.mark.slow
def test_train_forecast_imports_run_training():
    from scripts.train_forecast import run_training

    assert callable(run_training)
