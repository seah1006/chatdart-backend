"""
PDF 리포트 생성 서비스

fpdf2 라이브러리로 재무 분석 요약 PDF를 생성합니다.
한글 폰트 탐색 순서:
  1. /usr/share/fonts/truetype/nanum/NanumGothic.ttf  (Docker/Linux)
  2. C:/Windows/Fonts/malgun.ttf                       (Windows)
"""
import os
from datetime import datetime, timezone
from io import BytesIO
from typing import TYPE_CHECKING
from fpdf import FPDF
from fpdf.enums import XPos, YPos

from app.schemas.finance_schema import CompanySummary, CompareResponse

if TYPE_CHECKING:
    from app.db.models import AIAnalysisCache

_FONT_PATHS = [
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "C:/Windows/Fonts/malgun.ttf",
    "C:/Windows/Fonts/gulim.ttc",
]


def _find_korean_font() -> str | None:
    for path in _FONT_PATHS:
        if os.path.exists(path):
            return path
    return None


def _fmt_krw(value: int | None) -> str:
    """원 단위 정수를 억/조 단위 문자열로 변환. None이면 'N/A' 반환."""
    if value is None:
        return "N/A"
    if abs(value) >= 1_000_000_000_000:
        return f"{value / 1_000_000_000_000:.2f}조원"
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:.0f}억원"
    return f"{value:,}원"


def build_summary_pdf(summary: CompanySummary, ai_cache: "AIAnalysisCache | None" = None) -> bytes:
    """CompanySummary 데이터를 PDF 바이트로 변환합니다."""
    pdf = FPDF()
    pdf.set_creation_date(datetime(2000, 1, 1, tzinfo=timezone.utc))
    pdf.add_page()

    font_path = _find_korean_font()
    if font_path:
        pdf.add_font("Korean", fname=font_path)
        font_name = "Korean"
    else:
        font_name = "Helvetica"

    # ── 헤더 ──────────────────────────────────────────────────────────────────
    pdf.set_font(font_name, size=18)
    pdf.set_fill_color(30, 80, 160)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 14, f"  {summary.company_name} 재무 분석 리포트", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    # ── 기본 정보 ─────────────────────────────────────────────────────────────
    pdf.set_text_color(0, 0, 0)
    pdf.set_font(font_name, size=10)
    pdf.cell(0, 7, f"종목코드: {summary.stock_code}   |   분석 기준연도: {summary.fiscal_year}년", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    # ── 분석 지표 박스 ────────────────────────────────────────────────────────
    pdf.set_font(font_name, size=12)
    pdf.set_fill_color(240, 245, 255)
    pdf.cell(0, 9, "  종합 분석", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    pdf.set_font(font_name, size=10)
    status_rows = [
        ("성장성", summary.growth_status),
        ("안정성", summary.stability_status),
        ("수익성", summary.profitability_status),
    ]
    for label, value in status_rows:
        color = (34, 139, 34) if value not in ("부진", "주의", "데이터 부족") else (180, 0, 0)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(40, 8, f"  {label}")
        pdf.set_text_color(*color)
        pdf.cell(0, 8, value, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    if summary.metrics:
        pdf.set_font(font_name, size=12)
        pdf.set_fill_color(240, 245, 255)
        pdf.cell(0, 9, "  핵심 재무 지표 (최신 연도)", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(2)

        pdf.set_font(font_name, size=9)
        pdf.set_fill_color(60, 100, 180)
        pdf.set_text_color(255, 255, 255)
        pdf.cell(60, 7, " 지표", fill=True, border=1)
        pdf.cell(60, 7, " 수치", fill=True, border=1)
        pdf.ln()

        metrics_rows = [
            ("매출 성장률", summary.metrics.revenue_growth_rate),
            ("영업이익률", summary.metrics.operating_margin),
            ("순이익률", summary.metrics.net_margin),
            ("ROE (자기자본이익률)", summary.metrics.roe),
            ("ROA (총자산이익률)", summary.metrics.roa),
            ("부채비율", summary.metrics.debt_ratio),
        ]
        pdf.set_text_color(0, 0, 0)
        for idx, (label, value) in enumerate(metrics_rows):
            fill = idx % 2 == 0
            pdf.set_fill_color(248, 250, 255) if fill else pdf.set_fill_color(255, 255, 255)
            pdf.cell(60, 7, f"  {label}", fill=fill, border=1)
            text = f"  {value:.1f}%" if value is not None else "  N/A"
            pdf.cell(60, 7, text, fill=fill, border=1)
            pdf.ln()

        pdf.ln(4)

    # ── 연도별 재무 데이터 표 ─────────────────────────────────────────────────
    pdf.set_font(font_name, size=12)
    pdf.set_fill_color(240, 245, 255)
    pdf.cell(0, 9, "  연도별 재무 데이터", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    pdf.set_font(font_name, size=9)
    col_w = [22, 38, 38, 38, 38, 26]
    headers = ["연도", "매출액", "영업이익", "당기순이익", "총자산", "부채비율"]

    pdf.set_fill_color(60, 100, 180)
    pdf.set_text_color(255, 255, 255)
    for i, h in enumerate(headers):
        pdf.cell(col_w[i], 7, f" {h}", fill=True, border=1)
    pdf.ln()

    pdf.set_text_color(0, 0, 0)
    for i, row in enumerate(summary.history):
        fill = i % 2 == 0
        pdf.set_fill_color(248, 250, 255) if fill else pdf.set_fill_color(255, 255, 255)
        debt = (
            f"{row.total_liabilities / row.equity * 100:.0f}%"
            if (row.equity and row.total_liabilities is not None) else "N/A"
        )
        cells = [
            str(row.fiscal_year),
            _fmt_krw(row.revenue),
            _fmt_krw(row.operating_profit),
            _fmt_krw(row.net_income),
            _fmt_krw(row.total_assets),
            debt,
        ]
        for j, val in enumerate(cells):
            pdf.cell(col_w[j], 7, f" {val}", fill=fill, border=1)
        pdf.ln()

    pdf.ln(6)

    # ── 요약 텍스트 ───────────────────────────────────────────────────────────
    pdf.set_font(font_name, size=9)
    pdf.set_text_color(100, 100, 100)
    pdf.multi_cell(0, 6, summary.summary_text)

    if ai_cache is not None:
        pdf.ln(8)
        pdf.set_font(font_name, size=11)
        pdf.set_text_color(40, 40, 40)
        pdf.cell(0, 7, "AI 분석", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font(font_name, size=9)
        pdf.set_text_color(60, 60, 60)
        pdf.cell(
            0,
            6,
            f"다음해 예측 영업이익: {ai_cache.prediction:,.0f}",
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )
        pdf.ln(2)
        pdf.set_text_color(100, 100, 100)
        pdf.multi_cell(0, 6, ai_cache.summary)

    # ── 푸터 ──────────────────────────────────────────────────────────────────
    pdf.ln(6)
    pdf.set_font(font_name, size=8)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 6, "본 리포트는 DART 전자공시 데이터를 기반으로 ChatDART가 자동 생성하였습니다.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    return bytes(pdf.output())


def build_compare_pdf(response: CompareResponse) -> bytes:
    """CompareResponse 데이터를 비교 리포트 PDF 바이트로 변환합니다."""
    pdf = FPDF()
    pdf.set_creation_date(datetime(2000, 1, 1, tzinfo=timezone.utc))
    pdf.add_page()

    font_path = _find_korean_font()
    if font_path:
        pdf.add_font("Korean", fname=font_path)
        font_name = "Korean"
    else:
        font_name = "Helvetica"

    companies = response.companies
    title_names = " vs ".join(c.company_name for c in companies)

    # ── 헤더 ──────────────────────────────────────────────────────────────────
    pdf.set_font(font_name, size=16)
    pdf.set_fill_color(30, 80, 160)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 12, f"  {title_names} 비교 리포트", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    pdf.set_text_color(0, 0, 0)
    pdf.set_font(font_name, size=9)
    compared_at = response.compared_at.strftime("%Y-%m-%d %H:%M UTC") if response.compared_at else ""
    pdf.cell(0, 6, f"비교 기준: {compared_at}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(4)

    # ── 핵심 지표 비교 표 ─────────────────────────────────────────────────────
    pdf.set_font(font_name, size=11)
    pdf.set_fill_color(240, 245, 255)
    pdf.cell(0, 8, "  핵심 재무 지표 비교", fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    LABEL_W = 28
    n = len(companies)
    val_w = (190 - LABEL_W) // n

    pdf.set_font(font_name, size=8)
    pdf.set_fill_color(60, 100, 180)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(LABEL_W, 7, " 지표", fill=True, border=1)
    for c in companies:
        pdf.cell(val_w, 7, f" {c.company_name[:9]}", fill=True, border=1)
    pdf.ln()

    metrics_rows = [
        ("매출 성장률", "revenue_growth_rate"),
        ("영업이익률", "operating_margin"),
        ("순이익률", "net_margin"),
        ("ROE", "roe"),
        ("ROA", "roa"),
        ("부채비율", "debt_ratio"),
    ]
    pdf.set_text_color(0, 0, 0)
    for idx, (label, field) in enumerate(metrics_rows):
        fill = idx % 2 == 0
        pdf.set_fill_color(248, 250, 255) if fill else pdf.set_fill_color(255, 255, 255)
        pdf.cell(LABEL_W, 7, f" {label}", fill=fill, border=1)
        for c in companies:
            val = getattr(c.metrics, field)
            text = f" {val:.1f}%" if val is not None else " N/A"
            pdf.cell(val_w, 7, text, fill=fill, border=1)
        pdf.ln()

    pdf.ln(5)

    # ── 기업별 연도별 재무 데이터 ─────────────────────────────────────────────
    col_w = [20, 35, 32, 32, 32, 22]
    row_h = 6
    for company in companies:
        pdf.set_font(font_name, size=10)
        pdf.set_fill_color(230, 238, 255)
        label = f"  {company.company_name} ({company.stock_code}) — {company.fiscal_year}년"
        if company.is_financial_sector:
            label += "  ※금융업"
        pdf.cell(0, 8, label, fill=True, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(1)

        pdf.set_font(font_name, size=8)
        pdf.set_fill_color(80, 120, 200)
        pdf.set_text_color(255, 255, 255)
        for j, h in enumerate(["연도", "매출액", "영업이익", "당기순이익", "총자산", "부채비율"]):
            pdf.cell(col_w[j], row_h, f" {h}", fill=True, border=1)
        pdf.ln()

        pdf.set_text_color(0, 0, 0)
        for i, row in enumerate(company.history):
            fill = i % 2 == 0
            pdf.set_fill_color(248, 250, 255) if fill else pdf.set_fill_color(255, 255, 255)
            debt = (
                f"{row.total_liabilities / row.equity * 100:.0f}%"
                if (row.equity and row.total_liabilities is not None) else "N/A"
            )
            for j, val in enumerate([
                str(row.fiscal_year),
                _fmt_krw(row.revenue),
                _fmt_krw(row.operating_profit),
                _fmt_krw(row.net_income),
                _fmt_krw(row.total_assets),
                debt,
            ]):
                pdf.cell(col_w[j], row_h, f" {val}", fill=fill, border=1)
            pdf.ln()
        pdf.ln(4)

    # ── 푸터 ──────────────────────────────────────────────────────────────────
    pdf.set_font(font_name, size=8)
    pdf.set_text_color(150, 150, 150)
    pdf.cell(0, 6, "본 리포트는 DART 전자공시 데이터를 기반으로 ChatDART가 자동 생성하였습니다.", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    return bytes(pdf.output())
