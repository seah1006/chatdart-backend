"""Export FinancialStatement rows into AI training CSV format."""
import pandas as pd

from app.db.database import SessionLocal
from app.db.models import FinancialStatement


WINDOW = 5
REQUIRED_COLS = [
    "company",
    "year",
    "revenue",
    "operating_profit",
    "net_income",
    "debt",
    "equity",
    "cash",
]


def make_window(df: pd.DataFrame, window: int = WINDOW) -> pd.DataFrame:
    """Build sliding-window samples for next-year operating profit."""
    missing = [column for column in REQUIRED_COLS if column not in df.columns]
    if missing:
        raise ValueError(f"입력 DataFrame에 필수 컬럼 누락: {missing}")

    rows = []
    for company in df["company"].unique():
        temp = df[df["company"] == company].sort_values("year").reset_index(drop=True)
        if len(temp) < window + 1:
            continue

        for i in range(len(temp) - window):
            chunk = temp.iloc[i:i + window]
            target = temp.iloc[i + window]
            row = {}
            for idx, rec in enumerate(chunk.itertuples(index=False)):
                row[f"revenue_{idx}"] = rec.revenue
                row[f"op_{idx}"] = rec.operating_profit
                row[f"net_{idx}"] = rec.net_income
                row[f"debt_{idx}"] = rec.debt
                row[f"equity_{idx}"] = rec.equity
                row[f"cash_{idx}"] = rec.cash
            row["next_operating_profit"] = target.operating_profit
            rows.append(row)

    if not rows:
        raise ValueError(
            "학습 샘플을 생성할 수 없습니다. "
            f"각 기업에 최소 {window + 1}개 연도 데이터가 필요합니다."
        )

    return pd.DataFrame(rows)


def export_from_db(out_path: str = "data/train.csv") -> None:
    db = SessionLocal()
    try:
        records = db.query(FinancialStatement).all()
        df = pd.DataFrame([
            {
                "company": record.company_id,
                "year": record.fiscal_year,
                "revenue": record.revenue or 0,
                "operating_profit": record.operating_profit or 0,
                "net_income": record.net_income or 0,
                "debt": record.total_liabilities or 0,
                "equity": record.equity or 0,
                "cash": record.cash or 0,
            }
            for record in records
        ])
    finally:
        db.close()

    windowed = make_window(df)
    windowed.to_csv(out_path, index=False)
    print(f"학습 데이터 저장: {out_path} ({len(windowed)} rows)")


if __name__ == "__main__":
    export_from_db()
