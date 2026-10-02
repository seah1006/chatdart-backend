"""실험 rule(operating-cash-flow-much-lower-than-net-income) 검증용 일회성 집계 (Phase 103).

전체 수집 기업의 비금융·흑자 연도에 대해 OCF/순이익 비율 분포를 집계한다. read-only.

실행: python scripts/ocf_ratio_report.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

THRESHOLDS = (0.3, 0.4, 0.5, 0.6)
MIN_NET_INCOME = 1e10  # 100억 원


def build_report(rows):
    """rows: (company_id, company_name, fiscal_year, net_income, operating_cash_flow) 목록.

    금융업·OCF None 행은 호출 전에 제외되어 있다고 가정한다.
    반환: {positive_years, thresholds: {t: {hits, count_without_floor}}, repeat_companies}
    hits는 현행 실험 rule 조건(순이익 ≥ 100억, OCF ≥ 0) 기준.
    """
    positive = [r for r in rows if r[3] is not None and r[3] > 0]

    def hits_under(threshold, min_ni):
        return [
            (cid, name, year, round(ocf / ni, 4))
            for cid, name, year, ni, ocf in positive
            if ni >= min_ni and ocf >= 0 and ocf / ni < threshold
        ]

    thresholds = {}
    for t in THRESHOLDS:
        with_floor = hits_under(t, MIN_NET_INCOME)
        thresholds[t] = {
            "hits": sorted(with_floor, key=lambda h: (h[0], h[2])),
            "count_without_floor": len(hits_under(t, 0)),
        }

    counts = {}
    for cid, name, _, _ in thresholds[0.5]["hits"]:
        counts[(cid, name)] = counts.get((cid, name), 0) + 1
    repeat = sorted((cid, name, n) for (cid, name), n in counts.items() if n >= 2)

    return {
        "positive_years": len(positive),
        "thresholds": thresholds,
        "repeat_companies": repeat,
    }


def main():
    from app.db.database import SessionLocal
    from app.db.models import FinancialStatement as FS

    db = SessionLocal()
    try:
        q = (
            db.query(FS.company_id, FS.company_name, FS.fiscal_year, FS.net_income, FS.operating_cash_flow)
            .filter(FS.operating_cash_flow.isnot(None))
            .filter(~FS.source_report.like("%[금융업%"))
        )
        rows = [tuple(r) for r in q.all()]
    finally:
        db.close()

    report = build_report(rows)

    print("# OCF/순이익 비율 집계 (비금융·흑자 연도, Phase 103)\n")
    print(f"- 분석 대상: 비금융 흑자 연도 {report['positive_years']}개\n")
    print("| 임계값 | 발생(순이익≥100억) | 발생(100억 필터 없음) |")
    print("|--------|-------------------|----------------------|")
    for t in THRESHOLDS:
        info = report["thresholds"][t]
        print(f"| < {t} | {len(info['hits'])} | {info['count_without_floor']} |")
    print("\n## < 0.5 발생 상세 (현행 실험 rule 조건)\n")
    print("| 종목코드 | 기업명 | 연도 | OCF/순이익 |")
    print("|----------|--------|------|-----------|")
    for cid, name, year, ratio in report["thresholds"][0.5]["hits"]:
        print(f"| {cid} | {name} | {year} | {ratio} |")
    print("\n## 반복 발생 종목 (< 0.5, 2개 연도 이상)\n")
    if report["repeat_companies"]:
        for cid, name, n in report["repeat_companies"]:
            print(f"- {cid} {name}: {n}개 연도")
    else:
        print("- 없음")


if __name__ == "__main__":
    main()
