"""LightGBM next operating profit forecast features.

Synced with AI server (skyhubstk/ai-server) for Phase 48.
"""
import numpy as np
import pandas as pd


FEATURE_COLUMNS = [
    "op_margin_0",
    "op_margin_1",
    "op_margin_2",
    "op_margin_3",
    "op_margin_4",
    "net_margin_0",
    "net_margin_1",
    "net_margin_2",
    "net_margin_3",
    "net_margin_4",
    "revenue_growth_1",
    "revenue_growth_2",
    "revenue_growth_3",
    "revenue_growth_4",
    "op_growth_1",
    "op_growth_2",
    "op_growth_3",
    "op_growth_4",
    "revenue_cagr",
    "op_cagr",
    "debt_ratio_4",
    "equity_ratio_4",
    "debt_to_equity_4",
    "cash_to_revenue_4",
    "cash_to_debt_4",
    "roe_4",
    "roa_4",
    "op_margin_trend",
    "op_margin_std",
    "revenue_growth_std",
    "log_revenue_4",
    "log_assets_4",
]


FEATURE_COLUMNS_FIN = [
    "leverage_ratio_1",
    "equity_ratio_1",
    "roe_1",
    "roa_1",
    "asset_growth",
    "equity_growth",
    "net_growth",
    "log_assets_1",
    "roe_trend",
    "nii_ratio_1",
    "llp_ratio_1",
    "nii_growth",
    "ins_liab_ratio_1",
    "is_bank",
    "is_insurance",
    "is_securities",
]


def _safe_div(num, denom, fill=0.0):
    """Avoid divide-by-zero and inf values."""
    with np.errstate(divide="ignore", invalid="ignore"):
        numerator = np.asarray(num, dtype=float)
        denominator = np.asarray(denom, dtype=float)
        result = np.divide(
            numerator,
            denominator,
            out=np.full_like(numerator, fill, dtype=float),
            where=np.abs(denominator) >= 1e-9,
        )
    return float(result) if np.ndim(result) == 0 else result


def _safe_log(x, fill=0.0):
    """Log transform only positive values."""
    val = float(x)
    return np.log(val) if val > 0 else fill


def _growth_rate(new, old, fill=0.0):
    """Year-over-year growth rate."""
    return _safe_div(new - old, abs(old), fill)


def _cagr(end, start, years, fill=0.0):
    """Compound annual growth rate."""
    if start <= 0 or end <= 0:
        return fill
    return float((end / start) ** (1.0 / years) - 1)


def create_features(row: dict) -> dict:
    """Convert raw 5-year financial values into model features."""
    revenue = [row[f"revenue_{i}"] for i in range(5)]
    op = [row[f"op_{i}"] for i in range(5)]
    net = [row[f"net_{i}"] for i in range(5)]
    debt = [row[f"debt_{i}"] for i in range(5)]
    equity = [row[f"equity_{i}"] for i in range(5)]
    cash = [row[f"cash_{i}"] for i in range(5)]
    total_assets = [debt[i] + equity[i] for i in range(5)]

    features = {}

    for i in range(5):
        features[f"op_margin_{i}"] = _safe_div(op[i], revenue[i])
        features[f"net_margin_{i}"] = _safe_div(net[i], revenue[i])

    for i in range(1, 5):
        features[f"revenue_growth_{i}"] = _growth_rate(revenue[i], revenue[i - 1])
        features[f"op_growth_{i}"] = _growth_rate(op[i], op[i - 1])

    features["revenue_cagr"] = _cagr(revenue[4], revenue[0], years=4)
    features["op_cagr"] = _cagr(op[4], op[0], years=4)

    features["debt_ratio_4"] = _safe_div(debt[4], total_assets[4])
    features["equity_ratio_4"] = _safe_div(equity[4], total_assets[4])
    features["debt_to_equity_4"] = _safe_div(debt[4], equity[4])

    features["cash_to_revenue_4"] = _safe_div(cash[4], revenue[4])
    features["cash_to_debt_4"] = _safe_div(cash[4], debt[4])

    features["roe_4"] = _safe_div(net[4], equity[4])
    features["roa_4"] = _safe_div(net[4], total_assets[4])

    op_margins = [features[f"op_margin_{i}"] for i in range(5)]
    revenue_growths = [features[f"revenue_growth_{i}"] for i in range(1, 5)]
    features["op_margin_trend"] = op_margins[4] - op_margins[0]
    features["op_margin_std"] = float(np.std(op_margins))
    features["revenue_growth_std"] = float(np.std(revenue_growths))

    features["log_revenue_4"] = _safe_log(revenue[4])
    features["log_assets_4"] = _safe_log(total_assets[4])

    return {key: features[key] for key in FEATURE_COLUMNS}


def create_features_fin(row: dict) -> dict:
    """Create financial-sector inference features (window=2, idx 0=old / 1=latest)."""
    ta = [row.get(f"ta_{i}", 0.0) or 0.0 for i in range(2)]
    tl = [row.get(f"tl_{i}", 0.0) or 0.0 for i in range(2)]
    eq = [row.get(f"equity_{i}", 0.0) or 0.0 for i in range(2)]
    net = [row.get(f"net_{i}", 0.0) or 0.0 for i in range(2)]
    nii = [row.get(f"nii_{i}", 0.0) or 0.0 for i in range(2)]
    llp = [row.get(f"llp_{i}", 0.0) or 0.0 for i in range(2)]
    ins_liab = [row.get(f"ins_liab_{i}", 0.0) or 0.0 for i in range(2)]
    sector = str(row.get("sector_detail", "") or "")

    feats = {}
    feats["leverage_ratio_1"] = _safe_div(tl[1], ta[1])
    feats["equity_ratio_1"] = _safe_div(eq[1], ta[1])
    feats["roe_1"] = _safe_div(net[1], eq[1])
    feats["roa_1"] = _safe_div(net[1], ta[1])
    feats["asset_growth"] = _growth_rate(ta[1], ta[0])
    feats["equity_growth"] = _growth_rate(eq[1], eq[0])
    feats["net_growth"] = _growth_rate(net[1], net[0])
    feats["log_assets_1"] = _safe_log(ta[1])
    roe_0 = _safe_div(net[0], eq[0])
    feats["roe_trend"] = feats["roe_1"] - roe_0
    feats["nii_ratio_1"] = _safe_div(nii[1], ta[1])
    feats["llp_ratio_1"] = _safe_div(abs(llp[1]), ta[1])
    feats["nii_growth"] = _growth_rate(nii[1], nii[0])
    feats["ins_liab_ratio_1"] = _safe_div(ins_liab[1], ta[1])
    feats["is_bank"] = 1.0 if sector == "bank" else 0.0
    feats["is_insurance"] = 1.0 if sector == "insurance" else 0.0
    feats["is_securities"] = 1.0 if sector == "securities" else 0.0

    return {k: feats[k] for k in FEATURE_COLUMNS_FIN}


def create_features_df(df: pd.DataFrame) -> pd.DataFrame:
    """Apply create_features to a DataFrame."""
    rows = [create_features(row) for row in df.to_dict(orient="records")]
    return pd.DataFrame(rows, columns=FEATURE_COLUMNS)
