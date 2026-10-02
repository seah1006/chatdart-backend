from app.services.feature_engineering import (
    FEATURE_COLUMNS,
    FEATURE_COLUMNS_FIN,
    create_features,
    create_features_fin,
)


def _raw_row():
    row = {}
    for i in range(5):
        row[f"revenue_{i}"] = 1000.0 + i * 100.0
        row[f"op_{i}"] = 100.0 + i * 10.0
        row[f"net_{i}"] = 80.0 + i * 8.0
        row[f"debt_{i}"] = 400.0 + i * 20.0
        row[f"equity_{i}"] = 600.0 + i * 30.0
        row[f"cash_{i}"] = 50.0 + i * 5.0
    return row


def test_create_features_returns_all_32_columns_in_order():
    features = create_features(_raw_row())

    assert list(features.keys()) == FEATURE_COLUMNS
    assert len(features) == 32


def test_create_features_safe_div_zero_revenue():
    row = _raw_row()
    for i in range(5):
        row[f"revenue_{i}"] = 0.0

    features = create_features(row)

    for i in range(5):
        assert features[f"op_margin_{i}"] == 0.0


def test_create_features_negative_equity_cagr():
    row = _raw_row()
    row["op_0"] = -100.0
    row["op_4"] = 200.0

    features = create_features(row)

    assert features["op_cagr"] == 0.0


def test_create_features_fin_returns_16_columns():
    row = {
        "ta_0": 90.0,
        "ta_1": 100.0,
        "tl_0": 54.0,
        "tl_1": 60.0,
        "equity_0": 36.0,
        "equity_1": 40.0,
        "net_0": 6.0,
        "net_1": 8.0,
    }

    features = create_features_fin(row)

    assert list(features.keys()) == FEATURE_COLUMNS_FIN
    assert len(features) == 16


def test_create_features_fin_computes_known_values():
    row = {
        "ta_0": 80.0,
        "ta_1": 100.0,
        "tl_0": 50.0,
        "tl_1": 60.0,
        "equity_0": 30.0,
        "equity_1": 40.0,
        "net_0": 6.0,
        "net_1": 8.0,
    }

    features = create_features_fin(row)

    assert features["leverage_ratio_1"] == 0.6
    assert features["equity_ratio_1"] == 0.4
    assert features["roe_1"] == 0.2
    assert features["roa_1"] == 0.08
    assert features["asset_growth"] == 0.25
    assert features["roe_trend"] == 0.0


def test_create_features_fin_defaults_missing_fin_fields():
    row = {
        "ta_0": 80.0,
        "ta_1": 100.0,
        "tl_0": 50.0,
        "tl_1": 60.0,
        "equity_0": 30.0,
        "equity_1": 40.0,
        "net_0": 6.0,
        "net_1": 8.0,
    }

    features = create_features_fin(row)

    assert features["nii_ratio_1"] == 0.0
    assert features["llp_ratio_1"] == 0.0
    assert features["nii_growth"] == 0.0
    assert features["ins_liab_ratio_1"] == 0.0
    assert features["is_bank"] == 0.0
    assert features["is_insurance"] == 0.0
    assert features["is_securities"] == 0.0


def test_create_features_fin_uses_phase90_financial_fields():
    row = {
        "ta_0": 800.0,
        "ta_1": 1000.0,
        "tl_0": 500.0,
        "tl_1": 600.0,
        "equity_0": 300.0,
        "equity_1": 400.0,
        "net_0": 60.0,
        "net_1": 80.0,
        "nii_0": 25.0,
        "nii_1": 50.0,
        "llp_1": -5.0,
        "ins_liab_1": 400.0,
    }

    features = create_features_fin(row)

    assert features["nii_ratio_1"] == 0.05
    assert features["llp_ratio_1"] == 0.005
    assert features["nii_growth"] == 1.0
    assert features["ins_liab_ratio_1"] == 0.4


def test_create_features_fin_sector_one_hot():
    row = {
        "ta_0": 80.0,
        "ta_1": 100.0,
        "tl_0": 50.0,
        "tl_1": 60.0,
        "equity_0": 30.0,
        "equity_1": 40.0,
        "net_0": 6.0,
        "net_1": 8.0,
        "sector_detail": "bank",
    }

    features = create_features_fin(row)

    assert features["is_bank"] == 1.0
    assert features["is_insurance"] == 0.0
    assert features["is_securities"] == 0.0


def test_create_features_fin_handles_zero_denominator():
    row = {
        "ta_0": 0.0,
        "ta_1": 0.0,
        "tl_0": 0.0,
        "tl_1": 60.0,
        "equity_0": 0.0,
        "equity_1": 0.0,
        "net_0": 0.0,
        "net_1": 8.0,
    }

    features = create_features_fin(row)

    assert features["leverage_ratio_1"] == 0.0
    assert features["equity_ratio_1"] == 0.0
    assert features["roe_1"] == 0.0
    assert features["roa_1"] == 0.0
