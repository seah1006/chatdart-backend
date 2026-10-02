"""백테스트 패널로 현재 배포 예측 모델을 평가한다."""
from __future__ import annotations

import argparse
import csv
import math
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import numpy as np

from app.services import forecast_service
from app.services.feature_engineering import (
    FEATURE_COLUMNS,
    FEATURE_COLUMNS_FIN,
    create_features,
    create_features_fin,
)


BIAS_WARNING = """> 주의: AI팀 학습 split 이 공개되지 않아 이 표본이 학습에 포함됐을 가능성이 있다.
> SMAPE·정확도 절대값은 낙관 편향될 수 있으므로 참고값이다.
> 방향성 적중률과 베이스라인 대비 개선폭은 동일 표본 위 상대 비교라 이 편향의 영향이 작다."""
NUMERIC_COLUMNS = {
    "fiscal_year", "is_financial", "revenue", "operating_profit", "net_income",
    "total_assets", "total_liabilities", "equity", "cash", "net_interest_income",
    "loan_loss_provision", "insurance_liability",
}


def read_panel(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        for key in NUMERIC_COLUMNS:
            value = row.get(key)
            row[key] = None if value in (None, "") else int(float(value))
    return rows


def _missing(row: dict, fields: tuple[str, ...]) -> bool:
    return any(row.get(field) is None for field in fields)


def build_samples(rows: list[dict], model: str) -> tuple[list[dict], int, int, int]:
    financial = model == "fin"
    window = 2 if financial else 5
    required = (("total_assets", "total_liabilities", "equity", "net_income")
                if financial else
                ("revenue", "operating_profit", "net_income", "total_liabilities", "equity"))
    optional = (("net_interest_income", "loan_loss_provision", "insurance_liability")
                if financial else ("cash",))
    target = "net_income" if financial else "operating_profit"
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        if bool(row.get("is_financial")) != financial:
            continue
        grouped.setdefault(str(row["stock_code"]), []).append(row)

    samples = []
    excluded = 0
    zero_filled = sum(
        row.get(field) is None
        for company_rows in grouped.values()
        for row in company_rows
        for field in optional
    )
    gapped = 0
    for company_rows in grouped.values():
        company_rows.sort(key=lambda item: item["fiscal_year"])
        for start in range(len(company_rows) - window):
            history = company_rows[start:start + window]
            actual_row = company_rows[start + window]
            window_years = [row["fiscal_year"] for row in history]
            if (window_years != list(range(window_years[0], window_years[0] + window))
                    or actual_row["fiscal_year"] != window_years[-1] + 1):
                gapped += 1
                continue
            if any(_missing(row, required) for row in history) or actual_row.get(target) is None:
                excluded += 1
                continue
            if financial:
                raw = {}
                for idx, row in enumerate(history):
                    raw.update({
                        f"ta_{idx}": row["total_assets"], f"tl_{idx}": row["total_liabilities"],
                        f"equity_{idx}": row["equity"], f"net_{idx}": row["net_income"],
                        f"nii_{idx}": row["net_interest_income"] if row["net_interest_income"] is not None else 0,
                        f"llp_{idx}": row["loan_loss_provision"] if row["loan_loss_provision"] is not None else 0,
                        f"ins_liab_{idx}": row["insurance_liability"] if row["insurance_liability"] is not None else 0,
                    })
                sector_missing = not bool(history[-1].get("sector_detail"))
                raw["sector_detail"] = history[-1].get("sector_detail") or ""
                features = create_features_fin(raw)
                pred = forecast_service.predict_net_income(features, history[-1]["equity"])
                prev = history[-1]["net_income"]
            else:
                raw = {}
                for idx, row in enumerate(history):
                    raw.update({
                        f"revenue_{idx}": row["revenue"], f"op_{idx}": row["operating_profit"],
                        f"net_{idx}": row["net_income"], f"debt_{idx}": row["total_liabilities"],
                        f"equity_{idx}": row["equity"],
                        f"cash_{idx}": row["cash"] if row["cash"] is not None else 0,
                    })
                features = create_features(raw)
                pred = forecast_service.predict_op_profit(features, history[-1]["revenue"])
                prev = history[-1]["operating_profit"]
                sector_missing = False
            samples.append({"actual": float(actual_row[target]), "pred": float(pred),
                            "prev": float(prev), "sector_missing": sector_missing})
    return samples, excluded, zero_filled, gapped


def compute_metrics(samples: list[dict]) -> dict:
    if not samples:
        return {"n_samples": 0, "smape_excluded": 0, "direction_excluded": 0}
    actual = np.array([s["actual"] for s in samples], dtype=float)
    pred = np.array([s["pred"] for s in samples], dtype=float)
    prev = np.array([s["prev"] for s in samples], dtype=float)
    mae = float(np.mean(np.abs(actual - pred)))
    baseline_mae = float(np.mean(np.abs(actual - prev)))
    denom = np.abs(actual) + np.abs(pred)
    valid_smape = denom >= 1e-9
    smape = (float(np.mean(2 * np.abs(actual[valid_smape] - pred[valid_smape]) / denom[valid_smape]) * 100)
              if valid_smape.any() else math.nan)
    baseline_denom = np.abs(actual) + np.abs(prev)
    valid_baseline = baseline_denom >= 1e-9
    baseline_smape = (float(np.mean(2 * np.abs(actual[valid_baseline] - prev[valid_baseline]) /
                                    baseline_denom[valid_baseline]) * 100)
                      if valid_baseline.any() else math.nan)
    direction_valid = actual != prev
    direction_hit = (float(np.mean(np.sign(pred[direction_valid] - prev[direction_valid]) ==
                                   np.sign(actual[direction_valid] - prev[direction_valid])) * 100)
                     if direction_valid.any() else math.nan)
    improvement = ((baseline_mae - mae) / baseline_mae * 100
                   if baseline_mae >= 1e-9 else math.nan)
    return {
        "n_samples": len(samples), "mae": mae, "smape": smape, "accuracy": 100 - smape,
        "direction_hit": direction_hit, "baseline_mae": baseline_mae,
        "baseline_smape": baseline_smape, "improvement": improvement,
        "smape_excluded": int((~valid_smape).sum()),
        "direction_excluded": int((~direction_valid).sum()),
    }


def _fmt(value, suffix="") -> str:
    return "N/A" if value is None or math.isnan(value) else f"{value:,.2f}{suffix}"


def _metric_table(title: str, samples: list[dict], excluded: int, zero_filled: int,
                  gapped: int, exclusion_label: str = "필수 결측 제외",
                  show_zero_fill: bool = True, show_gap: bool = True) -> str:
    metrics = compute_metrics(samples)
    summary = f"- 유효 샘플: {metrics['n_samples']}개 / {exclusion_label}: {excluded}개"
    if show_gap:
        summary += f" / 연도 불연속 제외: {gapped}개"
    if show_zero_fill:
        summary += f" / 보조 필드 결측(0 대체 대상): {zero_filled}건"
    lines = [f"### {title}", "", summary]
    if not samples:
        return "\n".join(lines)
    lines += [
        "", "| 지표 | 값 |", "|------|----|",
        f"| MAE | {_fmt(metrics['mae'])}원 ({_fmt(metrics['mae'] / 1e12)}조원) |",
        f"| SMAPE | {_fmt(metrics['smape'], '%')} (분모 0 제외 {metrics['smape_excluded']}건) |",
        f"| 정확도¹ | {_fmt(metrics['accuracy'], '%')} |",
        f"| 방향성 적중률 | {_fmt(metrics['direction_hit'], '%')} (판정 제외 {metrics['direction_excluded']}건) |",
        f"| 베이스라인 MAE | {_fmt(metrics['baseline_mae'])}원 |",
        f"| 베이스라인 SMAPE | {_fmt(metrics['baseline_smape'], '%')} |",
        f"| 베이스라인 대비 개선폭 | {_fmt(metrics['improvement'], '%')} |",
    ]
    return "\n".join(lines)


def build_report(results: dict[str, tuple[list[dict], int, int, int]], unknown_financial: int = 0) -> str:
    lines = ["# 예측 모델 백테스트", "", BIAS_WARNING, "",
             "¹ 정확도 = 100 − SMAPE이며 발표 슬라이드(60.5 / 52.6)와 같은 정의입니다."]
    if unknown_financial:
        lines += ["", f"- is_financial 결측으로 제외된 패널 행: {unknown_financial}개"]
    for model, (samples, excluded, zero_filled, gapped) in results.items():
        label = "금융" if model == "fin" else "비금융"
        count = len(FEATURE_COLUMNS_FIN) if model == "fin" else len(FEATURE_COLUMNS)
        lines += ["", f"## {label} 모델 ({count}개 피처)", "",
                  _metric_table("전체 샘플", samples, excluded, zero_filled, gapped), ""]
        if gapped:
            lines += ["> 주의: 연도가 끊긴 구간 " + str(gapped) + "개를 제외했다. 패널에 수집 실패 연도가 있다는 뜻이며,",
                      "> 유효 샘플 수가 기대보다 적다면 --live 수집 로그에서 실패 연도를 먼저 확인한다.", ""]
        positive = [s for s in samples if s["actual"] > 0 and s["prev"] > 0]
        lines.append(_metric_table("흑자 구간(actual > 0 and prev > 0)", positive,
                                   len(samples) - len(positive), zero_filled, 0,
                                   "흑자 필터 제외", False, False))
        if model == "fin":
            sector_missing = sum(sample["sector_missing"] for sample in samples)
            ratio = sector_missing / len(samples) * 100 if samples else 0.0
            sector_line = (f"- sector_detail 결측: {sector_missing}개 샘플 / "
                           f"전체 {len(samples)}개 ({ratio:.1f}%)")
            if sector_missing:
                sector_line += " (업종 플래그 is_bank/is_insurance/is_securities 가 모두 0 으로 측정됨)"
            lines += ["", sector_line]
            if sector_missing:
                lines += ["", "> 주의: sector_detail 이 빈 샘플은 업종 구분 없이 예측된 값이다. 운영 경로는 이 경우 종목코드로",
                          "> 업종을 분류해 채우므로, 위 금융 지표는 운영 성능보다 낮게 나왔을 수 있다.",
                          "> 해소하려면 금융 종목을 재수집해 DB `sector_detail` 을 채우거나, `--live` 로 수집한 패널을 쓴다."]
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="예측 모델 백테스트")
    parser.add_argument("--panel", type=Path, required=True)
    parser.add_argument("--model", choices=("all", "nonfin", "fin"), default="all")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    rows = read_panel(args.panel)
    unknown_financial = sum(row.get("is_financial") is None for row in rows)
    rows = [row for row in rows if row.get("is_financial") is not None]
    requested = ["nonfin", "fin"] if args.model == "all" else [args.model]
    results = {}
    skipped = []
    for model in requested:
        model_path = forecast_service.MODEL_PATH_FIN if model == "fin" else forecast_service.MODEL_PATH
        if not model_path.exists():
            skipped.append(model)
            continue
        samples, excluded, zero_filled, gapped = build_samples(rows, model)
        if model == "nonfin" and not samples:
            message = (f"비금융 샘플 0개 (필수 결측 제외 {excluded}개 / "
                       f"연도 불연속 제외 {gapped}개)")
            if excluded == 0 and gapped == 0:
                message += " — 패널에 기업당 6개년 이상이 필요합니다"
            print(message)
        results[model] = (samples, excluded, zero_filled, gapped)
    print(build_report(results, unknown_financial))
    for model in skipped:
        print(f"\n## {'금융' if model == 'fin' else '비금융'} 모델\n\n모델 파일 없음 — 건너뜀")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
