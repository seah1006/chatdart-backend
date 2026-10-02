"""백테스트 패널 생성용 오프라인 분석 도구.

운영 경로와 동일한 정규화를 위해 DartService 내부 수집/보정 함수를 경유한다.
DB에는 쓰지 않으며, DART 호출은 --live를 명시한 경우에만 수행한다.
"""
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


CSV_COLUMNS = [
    "stock_code", "company_name", "fiscal_year", "is_financial",
    "revenue", "operating_profit", "net_income", "total_assets",
    "total_liabilities", "equity", "cash", "net_interest_income",
    "loan_loss_provision", "insurance_liability", "sector_detail",
    "source_report",
]


def _row_from_mapping(row: dict) -> dict:
    source_report = row.get("source_report") or ""
    return {
        "stock_code": row.get("company_id"),
        "company_name": row.get("company_name"),
        "fiscal_year": row.get("fiscal_year"),
        "is_financial": 1 if "[금융업" in source_report else 0,
        **{key: row.get(key) for key in CSV_COLUMNS[4:-1]},
        "source_report": source_report,
    }


def write_panel(rows: list[dict], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(_row_from_mapping(row) for row in rows)


def export_from_db(output: Path, session_factory=None) -> tuple[int, int]:
    from app.db.database import SessionLocal
    from app.db.models import FinancialStatement

    factory = session_factory or SessionLocal
    db = factory()
    try:
        records = db.query(FinancialStatement).order_by(
            FinancialStatement.company_id, FinancialStatement.fiscal_year
        ).all()
        rows = [{column.name: getattr(record, column.name) for column in FinancialStatement.__table__.columns}
                for record in records]
    finally:
        db.close()
    write_panel(rows, output)
    return len(rows), len({row["company_id"] for row in rows})


def _stock_codes_from_db(session_factory=None) -> list[str]:
    from app.db.database import SessionLocal
    from app.db.models import FinancialStatement

    factory = session_factory or SessionLocal
    db = factory()
    try:
        values = db.query(FinancialStatement.company_id).distinct().order_by(
            FinancialStatement.company_id
        ).all()
        return [value[0] for value in values]
    finally:
        db.close()


def _source_report(company_name: str, latest_year: int, is_financial: bool, sector_detail: str) -> str:
    if not is_financial:
        return f"{company_name} {latest_year}년 사업보고서"
    detail = sector_detail or "금융업"
    return f"{company_name} {latest_year}년 사업보고서 [금융업: {detail}]"


def _year_chunks(years: list[int]) -> list[list[int]]:
    """최신 연도부터 최대 5개년씩 묶어 fast-path 전제를 유지한다."""
    return [years[max(0, end - 5):end] for end in range(len(years), 0, -5)]


def collect_live(stock_codes: list[str], years: list[int], output: Path) -> tuple[int, int]:
    from app.services.dart_service import DartService

    DartService.initialize()
    rows = []
    failed_chunks = 0
    for stock_code in stock_codes:
        corp = DartService._global_corp_list.find_by_stock_code(stock_code)
        if corp is None:
            print(f"[건너뜀] {stock_code}: DART 기업 목록에 없음")
            continue
        is_financial = DartService._is_financial_sector(corp)
        concepts = DartService.FINANCIAL_CONCEPT_MAPPING if is_financial else DartService.CONCEPT_MAPPING
        names = (DartService.FINANCIAL_ACCOUNT_NAME_MAPPING if is_financial
                 else DartService.GENERAL_ACCOUNT_NAME_MAPPING)
        extracted = {}
        for chunk in _year_chunks(years):
            try:
                extracted.update(DartService._collect_financial_data(corp, concepts, chunk, names))
            except Exception as exc:
                failed_chunks += 1
                print(f"[경고] {stock_code} {chunk[0]}~{chunk[-1]} 수집 실패: {exc}")
        if not extracted:
            print(f"[건너뜀] {stock_code}: 전 연도 수집 실패")
            continue
        sector_detail = DartService.classify_financial_sector(stock_code) if is_financial else ""
        rows.extend(DartService._apply_corrections(
            extracted, corp.corp_name, stock_code,
            _source_report(corp.corp_name, years[-1], is_financial, sector_detail), is_financial,
        ))
    write_panel(rows, output)
    companies = len({row["company_id"] for row in rows})
    print(f"[요약] {len(stock_codes)}종목 중 {companies}종목 수집 / "
          f"실패 청크 {failed_chunks}건 (위 경고 로그 참조)")
    return len(rows), companies


def probe(stock_code: str, years: list[int]) -> None:
    from app.services.dart_service import DartService

    DartService.initialize()
    corp = DartService._global_corp_list.find_by_stock_code(stock_code)
    if corp is None:
        raise ValueError(f"DART 기업 목록에서 {stock_code}을 찾을 수 없습니다.")
    is_financial = DartService._is_financial_sector(corp)
    concepts = DartService.FINANCIAL_CONCEPT_MAPPING if is_financial else DartService.CONCEPT_MAPPING
    names = (DartService.FINANCIAL_ACCOUNT_NAME_MAPPING if is_financial
             else DartService.GENERAL_ACCOUNT_NAME_MAPPING)
    extracted = {}
    for chunk in _year_chunks(years):
        try:
            extracted.update(DartService._collect_financial_data(corp, concepts, chunk, names))
        except Exception as exc:
            print(f"[경고] {chunk[0]}~{chunk[-1]} 수집 실패: {exc}")
    print("| 연도 | revenue | operating_profit | 판정 |")
    print("|------|---------|------------------|------|")
    for year in years:
        data = extracted.get(year) or {}
        revenue = data.get("revenue")
        op = data.get("operating_profit")
        verdict = "정상" if data else "미제공"
        print(f"| {year} | {revenue if revenue is not None else '(없음)'} | "
              f"{op if op is not None else '(없음)'} | {verdict} |")


def _print_plan(stock_count: int | None, years: list[int], probe_code: str | None = None) -> None:
    chunk_count = math.ceil(len(years) / 5)
    calls_per_stock = chunk_count * 2
    target = 1 if probe_code else stock_count
    label = (f"probe {probe_code}" if probe_code else
             (f"대상 종목 {stock_count}개" if stock_count is not None else
              "대상 종목 (DB 미기동 — 종목 수 미확인)"))
    print(f"[dry-run] {label} / 연도 {years[0]}~{years[-1]} ({len(years)}개년)")
    total = calls_per_stock * target if target is not None else "미확인"
    print(f"[dry-run] 5년 청크 {chunk_count}개 × 종목당 OpenAPI 보고서 조회 최대 2회 = "
          f"종목당 최대 {calls_per_stock}회, 합계 최대 {total}회")
    print("[dry-run] 주의: 청크 내 미제공 연도는 dart-fss extract_fs 폴백"
          "(종목당 30~40초)이 추가로 발생합니다.")
    print("[dry-run] 실행하려면 --live 를 붙이세요. DART 실호출 0건으로 종료합니다.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="백테스트 패널 CSV 생성")
    parser.add_argument("--from-db", action="store_true", help="DB 전 행을 CSV로 내보냄")
    parser.add_argument("--live", action="store_true", help="DART 실호출 허용")
    parser.add_argument("--from-year", type=int, default=2016)
    parser.add_argument("--probe", metavar="STOCK_CODE")
    parser.add_argument("--stocks", metavar="CODES",
                        help="쉼표로 구분한 6자리 종목코드. 지정 시 DB 종목 목록 대신 사용")
    parser.add_argument("--out", type=Path, default=Path("data/backtest_panel.csv"))
    return parser


def main(argv=None, session_factory=None) -> int:
    args = build_parser().parse_args(argv)
    if args.from_db:
        if args.live or args.from_year != 2016 or args.probe or args.stocks:
            print("[경고] --from-db와 함께 지정한 --live/--from-year/--probe/--stocks 옵션은 무시합니다.")
        try:
            rows, companies = export_from_db(args.out, session_factory)
        except Exception as exc:
            print(f"[오류] DB 조회 실패: {exc}")
            return 1
        print(f"패널 생성 완료: {args.out} / {rows}행 / {companies}개사")
        return 0

    from app.services.dart_service import DartService
    latest = DartService._latest_fiscal_year()
    if args.from_year > latest:
        raise ValueError(f"--from-year는 최신 사업연도({latest}) 이하여야 합니다.")
    years = list(range(args.from_year, latest + 1))
    if args.probe:
        stock_codes = [args.probe]
    elif args.stocks is not None:
        stock_codes = []
        for value in args.stocks.split(","):
            code = value.strip()
            if len(code) == 6 and code.isdigit():
                stock_codes.append(code)
            else:
                print(f"[경고] 무시: {code}")
    else:
        try:
            stock_codes = _stock_codes_from_db(session_factory)
        except Exception:
            if args.live:
                raise
            stock_codes = None
    if not args.live:
        _print_plan(len(stock_codes) if stock_codes is not None else None, years, args.probe)
        return 0
    if args.probe:
        probe(args.probe, years)
        return 0
    rows, companies = collect_live(stock_codes or [], years, args.out)
    print(f"패널 생성 완료: {args.out} / {rows}행 / {companies}개사")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
