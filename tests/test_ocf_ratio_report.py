"""Phase 103 — scripts/ocf_ratio_report.py build_report 테스트."""
from scripts.ocf_ratio_report import build_report

_ROWS = [
    # (company_id, company_name, fiscal_year, net_income, operating_cash_flow)
    ("035720", "카카오", 2022, 1e12, 4.995e11),       # ratio 0.4995 — 0.5 경계 바로 아래
    ("068270", "셀트리온", 2022, 5.426e11, 8.6e8),    # ratio 0.0016
    ("035420", "NAVER", 2021, 1.649e13, 1.380e12),    # ratio 0.0837
    ("005930", "삼성전자", 2024, 3.445e13, 7.298e13), # ratio > 1 — 미발생
    ("000660", "SK하이닉스", 2023, -9.14e12, 4.28e12),# 적자 — 분석 제외
    ("099999", "소형사", 2023, 5e9, 1e9),             # ratio 0.2 — 순이익 100억 미만
    ("068270", "셀트리온", 2023, 5.397e11, 1.5e11),   # ratio 0.2779 — 반복 발생용
]


def test_build_report_thresholds():
    report = build_report(_ROWS)

    # 흑자 연도만 분석 대상 (적자 1건 제외)
    assert report["positive_years"] == 6

    # 순이익 ≥ 100억 조건: 소형사(0.2)는 제외됨
    assert len(report["thresholds"][0.3]["hits"]) == 3   # 셀트리온 x2, NAVER
    assert len(report["thresholds"][0.4]["hits"]) == 3
    assert len(report["thresholds"][0.5]["hits"]) == 4   # + 카카오 0.4995
    assert len(report["thresholds"][0.6]["hits"]) == 4

    # 100억 필터 미적용 시 소형사 포함
    assert report["thresholds"][0.3]["count_without_floor"] == 4

    # 경계 사례 ratio 검증
    kakao = [h for h in report["thresholds"][0.5]["hits"] if h[0] == "035720"]
    assert kakao and kakao[0][3] == 0.4995

    # 반복 발생: 셀트리온 2개 연도
    assert report["repeat_companies"] == [("068270", "셀트리온", 2)]
