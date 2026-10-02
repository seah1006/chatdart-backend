import logging
import math
import re
import json
from collections import defaultdict
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query, Request, Response, Path
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter
from sqlalchemy.orm import Session
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from typing import Dict, List, Optional
from datetime import datetime, timedelta, timezone

from app.db.database import get_db
from app.core.config import settings
from app.core.security import mask_email_for_log
logger = logging.getLogger(__name__)
from app.db.models import AIAnalysisCache, CollectionTask, FinancialStatement, SearchLog, CompareShare
from app.schemas.finance_schema import (
    CompanySearchItem, CompanySummary, YearlyData, Metrics,
    CollectStartResponse, CollectStatusResponse, CollectRefreshResponse,
    CompanyCompareItem, CompareInsightResponse, CompareResponse, CollectedCompany,
    CompareShareResponse, PopularCompany, PopularCompanyItem, PopularSearchResponse,
    BatchCollectItem, BatchCollectResponse, DeleteCompanyResponse,
    BatchStatusItem, TrendResponse, CompanyDetailResponse, AIAnalysisResponse, SectorRank,
    SimilarCompanyItem, RecommendedCompanyItem,
    InsightInterpretRequest, InsightInterpretResponse, InsightInterpretation,
    DisclosureItem, DisclosureListResponse,
)
from app.services.analysis import (
    evaluate_growth, evaluate_stability, evaluate_profitability,
    compute_interpretation_points, compute_metrics, compute_warning_flags,
)
from app.services.ai_summary_service import (
    INSIGHT_PROMPT_VERSION,
    build_prediction_display_text,
    canonical_prediction,
    interpret_insights,
    summarize,
    summarize_compare,
)
from app.services import ai_analysis_service
from app.services.dart_service import DartService, ServiceNotReadyError, CompanyNotFoundError
from app.services.feature_engineering import create_features, create_features_fin
from app.services.forecast_service import predict_op_profit, predict_net_income
from app.db.database import SessionLocal
from app.services.pdf_service import build_summary_pdf, build_compare_pdf
from app.dependencies.auth import require_auth, get_admin_user
from app.dependencies.rate_limit import limiter

router = APIRouter(prefix="/api/v1/finance", tags=["Finance"])
KEYWORD_PATTERN = re.compile(r"^[가-힣a-zA-Z0-9\s\-&]+$")
_SUMMARY_YEAR_PATTERN = re.compile(r"(?:19|20)\d{2}년")


def _summary_years_are_valid(summary: str, base_year: int, forecast_year: int) -> bool:
    years = {int(match[:4]) for match in _SUMMARY_YEAR_PATTERN.findall(summary)}
    return years.issubset({base_year, forecast_year})


def _summary_prediction_violations(
    summary: str, base_year: int, forecast_year: int, display_text: str,
) -> list[str]:
    violations = []
    if display_text not in summary:
        violations.append("display_missing")

    sentences = []
    for line in summary.splitlines():
        sentences.extend(re.split(r"(?<=[.!?])\s+", line))
    display_pattern = re.escape(display_text)
    for sentence in sentences:
        for match in re.finditer(display_pattern, sentence):
            before = sentence[:match.start()]
            years = list(re.finditer(r"(?:19|20)\d{2}년", before))
            nearest_year = int(years[-1].group()[:4]) if years else None
            if f"{forecast_year}년" not in sentence or nearest_year != forecast_year:
                if "prediction_year" not in violations:
                    violations.append("prediction_year")

    remainder = summary.replace(display_text, "")
    if re.search(r"\d[\d,]*(?:\.\d+)?\s*(?:조|억|만|천)?\s*원", remainder):
        violations.append("extra_amount")
    return violations


def _write_search_log(stock_code: str, company_name: str, user_id: Optional[int]) -> None:
    """SearchLog를 별도 세션으로 기록. BackgroundTasks 전용."""
    db = SessionLocal()
    try:
        db.add(SearchLog(
            stock_code=stock_code,
            company_name=company_name,
            searched_at=datetime.now(timezone.utc),
            user_id=user_id,
        ))
        db.commit()
    except Exception:
        db.rollback()
        logger.warning("SearchLog 저장 실패 stock_code=%s", stock_code, exc_info=True)
    finally:
        db.close()


async def _collect_with_email(stock_code: str, user_email: Optional[str]) -> None:
    """수집 완료 후 유저에게 이메일 알림. user_email이 없으면 스킵."""
    import asyncio
    try:
        await asyncio.to_thread(DartService.fetch_and_process_data, stock_code)
    except Exception as e:
        # fetch_and_process_data는 내부에서 CollectionTask를 failed로 업데이트하고 예외를 삼킨다.
        # 방어적으로 감싸 BackgroundTasks 핸들러로 예외가 전파되지 않도록 한다.
        logger.error("_collect_with_email 수집 실패 [%s]: %s", stock_code, e, exc_info=True)
        return
    if user_email:
        try:
            from app.services.email_service import send_collect_complete
            company_name = DartService.get_company_name_by_stock_code(stock_code) or stock_code
            await send_collect_complete(user_email, company_name, stock_code)
        except Exception as e:
            logger.warning(
                "수집 완료 이메일 발송 실패 [%s → email_hash=%s]: %s",
                stock_code,
                mask_email_for_log(user_email),
                e,
            )


def _claim_collection_task(stock_code: str, db: Session) -> bool:
    """CollectionTask를 processing으로 원자적 선점. True면 호출자가 수집을 시작한다.

    선점 성공 시 내부에서 즉시 commit한다. 호출부는 추가 commit이 필요 없다.
    동시 INSERT 경쟁으로 IntegrityError가 발생하면 rollback 후 False를 반환한다.
    """
    now = datetime.now(timezone.utc)
    updated = (
        db.query(CollectionTask)
        .filter(
            CollectionTask.stock_code == stock_code,
            CollectionTask.status != "processing",
        )
        .update(
            {"status": "processing", "message": None, "started_at": now},
            synchronize_session=False,
        )
    )
    if updated:
        db.commit()
        return True

    existing = (
        db.query(CollectionTask.stock_code)
        .filter(CollectionTask.stock_code == stock_code)
        .first()
    )
    if existing:
        return False

    try:
        db.add(CollectionTask(stock_code=stock_code, status="processing", started_at=now))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _has_financial_data(stock_code: str, db: Session) -> bool:
    return (
        db.query(FinancialStatement.company_id)
        .filter(FinancialStatement.company_id == stock_code)
        .first()
        is not None
    )


def _is_completed_with_financial_data(stock_code: str, db: Session) -> bool:
    task = db.query(CollectionTask).filter(CollectionTask.stock_code == stock_code).first()
    return bool(task and task.status == "completed" and _has_financial_data(stock_code, db))


def _silent_refresh(stock_code: str) -> None:
    """TTL 만료 시 사용자 응답 후 조용히 재수집. BackgroundTasks 전용."""
    db = SessionLocal()
    try:
        if not _claim_collection_task(stock_code, db):
            return
    except Exception:
        db.rollback()
        return
    finally:
        db.close()
    try:
        DartService.fetch_and_process_data(stock_code)
    except Exception as e:
        logger.error("_silent_refresh 수집 실패 [%s]: %s", stock_code, e, exc_info=True)


def _fmt_amount(value) -> str:
    """금액 포맷. None이면 'N/A', 0이면 '0원'."""
    return f"{value:,}원" if value is not None else "N/A"


def _enrich_history(records: list, unit_divisor: int = 1) -> list:
    """FinancialStatement 목록을 YearlyData로 변환. 단위 변환 및 비율 지표 계산 포함."""
    def scale(v):
        return round(v / unit_divisor) if v is not None else None

    def pct(num, den):
        if num is None or not den:
            return None
        return round(num / den * 100, 2)

    result = []
    for i, r in enumerate(records):
        prev = records[i + 1] if i + 1 < len(records) else None
        growth = None
        if prev and prev.revenue and r.revenue is not None:
            growth = round((r.revenue - prev.revenue) / abs(prev.revenue) * 100, 2)

        result.append(YearlyData(
            fiscal_year=r.fiscal_year,
            revenue=scale(r.revenue),
            cost_of_sales=scale(r.cost_of_sales),
            gross_profit=scale(r.gross_profit),
            sga=scale(r.sga),
            operating_profit=scale(r.operating_profit),
            net_income=scale(r.net_income),
            total_assets=scale(r.total_assets),
            total_liabilities=scale(r.total_liabilities),
            equity=scale(r.equity),
            cash=scale(r.cash),
            other_income=scale(r.other_income),
            other_expense=scale(r.other_expense),
            finance_income=scale(r.finance_income),
            finance_cost=scale(r.finance_cost),
            income_before_tax=scale(r.income_before_tax),
            income_tax_expense=scale(r.income_tax_expense),
            operating_cash_flow=scale(r.operating_cash_flow),
            net_interest_income=scale(r.net_interest_income),
            loan_loss_provision=scale(r.loan_loss_provision),
            insurance_liability=scale(r.insurance_liability),
            sector_detail=r.sector_detail,
            source_report=r.source_report,
            operating_margin=pct(r.operating_profit, r.revenue),
            net_margin=pct(r.net_income, r.revenue),
            revenue_growth_rate=growth,
        ))
    return result


def _compute_avg_metrics(records: list) -> Metrics:
    """FinancialStatement 목록의 평균 Metrics를 반환. None 필드는 집계에서 제외."""
    def avg(values):
        valid = [v for v in values if v is not None]
        return round(sum(valid) / len(valid), 2) if valid else None

    def pct(num_field, den_field):
        values = []
        for r in records:
            num = getattr(r, num_field)
            den = getattr(r, den_field)
            if num is not None and den and den > 0:
                values.append(num / den * 100)
        return avg(values)

    return Metrics(
        revenue_growth_rate=None,
        operating_margin=pct("operating_profit", "revenue"),
        net_margin=pct("net_income", "revenue"),
        roe=pct("net_income", "equity"),
        roa=pct("net_income", "total_assets"),
        debt_ratio=pct("total_liabilities", "equity"),
    )


def _fetch_latest_two_per_company(
    db: Session,
    company_ids: Optional[List[str]] = None,
    created_after: Optional[datetime] = None,
    record_limit: int = 2,
) -> Dict[str, List[FinancialStatement]]:
    """Return newest FinancialStatement rows per company_id."""
    query = db.query(FinancialStatement)
    if company_ids:
        query = query.filter(FinancialStatement.company_id.in_(company_ids))

    rows = query.order_by(
        FinancialStatement.company_id.asc(),
        FinancialStatement.fiscal_year.desc(),
    ).all()

    grouped: Dict[str, List[FinancialStatement]] = {}
    for row in rows:
        bucket = grouped.setdefault(row.company_id, [])
        if len(bucket) < record_limit:
            bucket.append(row)

    if created_after is not None:
        grouped = {
            company_id: records
            for company_id, records in grouped.items()
            if records
            and records[0].created_at
            and (
                records[0].created_at
                if records[0].created_at.tzinfo
                else records[0].created_at.replace(tzinfo=timezone.utc)
            ) >= created_after
        }

    return grouped


_SECTOR_CACHE: Dict[str, Optional[str]] = {}
_RECOMMENDATION_CACHE_TTL_SECONDS = 300
_recommendation_cache: Dict[int, Dict[str, object]] = {}
_SIMILAR_CACHE_TTL_SECONDS = 300
_similar_cache: Dict[tuple, Dict[str, object]] = {}
RECOMMENDATION_GROWTH_WEIGHT = 0.4
RECOMMENDATION_PROFITABILITY_WEIGHT = 0.4
RECOMMENDATION_STABILITY_WEIGHT = 0.2
RECOMMENDATION_RISK_PENALTY_PER_FLAG = 0.1
RECOMMENDATION_FATAL_FLAGS = ("자본잠식", "3년 연속 영업손실")
_GROWTH_SCORE = {"양호": 1.0, "보합": 0.5, "부진": 0.0, "데이터 부족": 0.0}
_PROFITABILITY_SCORE = {"개선": 1.0, "유지": 0.5, "부진": 0.0, "데이터 부족": 0.0}
_STABILITY_SCORE = {"우수": 1.0, "주의": 0.4, "위험": 0.0, "데이터 부족": 0.0}


def _safe_get_sector(stock_code: str) -> Optional[str]:
    if stock_code in _SECTOR_CACHE:
        return _SECTOR_CACHE[stock_code]
    try:
        value = DartService.get_sector_by_stock_code(stock_code)
    except Exception:
        value = None
    _SECTOR_CACHE[stock_code] = value
    return value


def _build_recommendation_items(
    db: Session,
    limit: int,
    now: Optional[datetime] = None,
) -> List[RecommendedCompanyItem]:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=90)
    groups = _fetch_latest_two_per_company(db, created_after=cutoff, record_limit=3)

    candidates: List[tuple[float, float, int, RecommendedCompanyItem]] = []
    for company_id, records in groups.items():
        latest = records[0]
        previous = records[1] if len(records) > 1 else None
        growth = evaluate_growth(latest, previous)
        stability = evaluate_stability(latest)
        profitability = evaluate_profitability(latest, previous)
        flags = compute_warning_flags(records)
        if any(fatal in flags for fatal in RECOMMENDATION_FATAL_FLAGS):
            continue
        healthy_count = sum([
            growth == "양호",
            stability == "우수",
            profitability == "개선",
        ])
        if healthy_count < 2:
            continue
        if not latest.revenue or latest.operating_profit is None:
            continue

        score = (
            RECOMMENDATION_GROWTH_WEIGHT * _GROWTH_SCORE.get(growth, 0.0)
            + RECOMMENDATION_PROFITABILITY_WEIGHT * _PROFITABILITY_SCORE.get(profitability, 0.0)
            + RECOMMENDATION_STABILITY_WEIGHT * _STABILITY_SCORE.get(stability, 0.0)
            - RECOMMENDATION_RISK_PENALTY_PER_FLAG * len(flags)
        )
        operating_margin_ratio = latest.operating_profit / latest.revenue
        sector = _safe_get_sector(company_id)
        candidates.append((
            score,
            operating_margin_ratio,
            latest.revenue,
            RecommendedCompanyItem(
                stock_code=company_id,
                company_name=latest.company_name or "",
                sector=sector,
                fiscal_year=latest.fiscal_year,
                revenue=latest.revenue,
                operating_profit=latest.operating_profit,
                operating_margin=round(operating_margin_ratio * 100, 2),
                growth_status=growth,
                profitability_status=profitability,
                stability_status=stability,
                healthy_count=healthy_count,
            ),
        ))

    candidates.sort(key=lambda item: (-item[0], -item[1], -item[2]))
    sector_counts: Dict[Optional[str], int] = {}
    result: List[RecommendedCompanyItem] = []
    for _, _, _, item in candidates:
        sector_count = sector_counts.get(item.sector, 0)
        if sector_count >= 2:
            continue
        sector_counts[item.sector] = sector_count + 1
        result.append(item)
        if len(result) >= limit:
            break

    return result


def warm_recommendation_cache(db: Session, limits: Optional[List[int]] = None) -> None:
    limits = limits or [5, 10]
    now = datetime.now(timezone.utc)
    for limit in limits:
        _recommendation_cache[limit] = {
            "value": _build_recommendation_items(db, limit, now=now),
            "expires_at": now + timedelta(seconds=_RECOMMENDATION_CACHE_TTL_SECONDS),
        }


def _build_company_summary(actual_code: str, records: list, unit_divisor: int = 1, db=None) -> CompanySummary:
    """FinancialStatement 목록으로 CompanySummary를 생성. get_summary·export 공통 사용."""
    latest = records[0]
    previous = records[1] if len(records) > 1 else None
    clean_name = latest.company_name or "알 수 없는 기업"
    is_financial = "[금융업" in (latest.source_report or "")
    growth = evaluate_growth(latest, previous)
    stability = evaluate_stability(latest)
    profitability = evaluate_profitability(latest, previous)
    flags = compute_warning_flags(records)
    metrics = compute_metrics(latest, previous)
    interpretation_points = compute_interpretation_points(records, metrics)

    if is_financial:
        nii = ""
        if latest.sector_detail == "bank" and latest.net_interest_income is not None:
            nii = f", 순이자이익 {_fmt_amount(latest.net_interest_income)}"
        summary_text = (
            f"[{clean_name} {latest.fiscal_year}년 결산 - 금융업] "
            f"총자산 {_fmt_amount(latest.total_assets)}, "
            f"당기순이익 {_fmt_amount(latest.net_income)}{nii}, "
            f"자기자본 {_fmt_amount(latest.equity)}\n"
            f"성장성 {growth} · 수익성 {profitability} · 안정성 {stability}"
        )
    else:
        sga_label, industry_note = (
            ("영업비용", "IT 서비스 업종 특성 통합 공시")
            if not latest.cost_of_sales   # None 또는 0 모두 처리
            else ("판관비", "제조업 표준 회계 기준 산출")
        )
        margin_str = (
            f" (영업이익률 {metrics.operating_margin:.1f}%)"
            if metrics.operating_margin is not None else ""
        )
        summary_text = (
            f"[{clean_name} {latest.fiscal_year}년 결산] "
            f"매출액 {_fmt_amount(latest.revenue)}, {sga_label} {_fmt_amount(latest.sga)}, "
            f"영업이익 {_fmt_amount(latest.operating_profit)}{margin_str}\n"
            f"성장성 {growth} · 수익성 {profitability} · 안정성 {stability}\n"
            f"※ {industry_note}"
        )
    if flags:
        summary_text += f"\n경고 {' / '.join(flags)}"

    sector = DartService.get_sector_by_stock_code(actual_code)
    sector_avg = None
    peer_records = []
    if db is not None and sector:
        completed_codes = [
            t.stock_code for t in
            db.query(CollectionTask.stock_code)
            .filter(CollectionTask.status == "completed")
            .all()
        ]
        peer_codes = [
            code for code in completed_codes
            if code != actual_code
            and DartService.get_sector_by_stock_code(code) == sector
        ]
        if peer_codes:
            latest_year_sq = (
                db.query(
                    FinancialStatement.company_id,
                    func.max(FinancialStatement.fiscal_year).label("max_year"),
                )
                .filter(FinancialStatement.company_id.in_(peer_codes))
                .group_by(FinancialStatement.company_id)
                .subquery()
            )
            peer_records = (
                db.query(FinancialStatement)
                .join(
                    latest_year_sq,
                    (FinancialStatement.company_id == latest_year_sq.c.company_id)
                    & (FinancialStatement.fiscal_year == latest_year_sq.c.max_year),
                )
                .all()
            )
            if peer_records:
                sector_avg = _compute_avg_metrics(peer_records)

    sector_rank = None
    if sector_avg is not None and peer_records:
        all_sector = peer_records + [latest]
        total = len(all_sector)

        rev_sorted = sorted(
            [r for r in all_sector if r.revenue is not None],
            key=lambda r: r.revenue,
            reverse=True,
        )
        rev_rank = next(
            (i + 1 for i, r in enumerate(rev_sorted) if r.company_id == actual_code),
            None,
        )

        def _op_margin(record):
            if record.operating_profit is not None and record.revenue:
                return record.operating_profit / record.revenue * 100
            return None

        om_sorted = sorted(
            [r for r in all_sector if _op_margin(r) is not None],
            key=_op_margin,
            reverse=True,
        )
        om_rank = next(
            (i + 1 for i, r in enumerate(om_sorted) if r.company_id == actual_code),
            None,
        )

        sector_rank = SectorRank(
            revenue_rank=rev_rank,
            operating_margin_rank=om_rank,
            total_companies=total,
        )

    # 프론트 표기용 메타 필드 (Phase 96) — 금융업은 revenue/operating_profit이 null이므로 라벨도 None
    if is_financial:
        industry_type = (
            latest.sector_detail
            if latest.sector_detail in ("bank", "insurance", "securities")
            else "financial"
        )
        revenue_label = None
        operating_profit_label = None
    else:
        industry_type = "general"
        revenue_label = "매출액"
        operating_profit_label = "영업이익"

    return CompanySummary(
        company_name=clean_name,
        stock_code=actual_code,
        fiscal_year=latest.fiscal_year,
        sector=sector,
        is_financial_sector=is_financial,
        industry_type=industry_type,
        revenue_label=revenue_label,
        operating_profit_label=operating_profit_label,
        summary_text=summary_text,
        growth_status=growth,
        stability_status=stability,
        profitability_status=profitability,
        metrics=metrics,
        warning_flags=flags,
        interpretation_points=interpretation_points,
        history=_enrich_history(records, unit_divisor),
        sector_avg=sector_avg,
        sector_rank=sector_rank,
    )


def get_resolved_code(keyword: str) -> str:
    """서비스의 순수 파이썬 예외를 잡아 HTTP 예외로 변환"""
    keyword = _normalize_keyword(keyword)
    try:
        return DartService.resolve_stock_code(keyword)
    except ServiceNotReadyError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except CompanyNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

def _normalize_keyword(keyword: str) -> str:
    return " ".join(keyword.split())


def _validate_keyword(keyword: str) -> str:
    keyword = _normalize_keyword(keyword)
    if not 1 <= len(keyword) <= 50 or not KEYWORD_PATTERN.fullmatch(keyword):
        raise HTTPException(status_code=422, detail="keyword 형식 오류")
    return keyword


def _validate_keywords(keywords: List[str], *, max_count: int) -> List[str]:
    if len(keywords) > max_count:
        raise HTTPException(status_code=422, detail=f"한 번에 최대 {max_count}개 기업까지 요청 가능합니다.")
    return [_validate_keyword(keyword) for keyword in keywords]


def _validate_keyword_item(keyword: str) -> tuple[str, str | None]:
    try:
        return _validate_keyword(keyword), None
    except HTTPException as e:
        return _normalize_keyword(keyword), str(e.detail)


KEYWORD_QUERY = Query(
    ...,
    min_length=1,
    max_length=50,
    pattern=r'^[가-힣a-zA-Z0-9\s\-&]+$',
    description="기업명 또는 6자리 종목코드"
)

@router.get(
    "/search",
    response_model=List[CompanySearchItem],
    summary="기업명 자동완성 검색",
    description="검색어를 포함하는 상장 기업 목록을 최대 10건 반환합니다. 기업명 또는 종목코드로 검색 가능합니다.",
    responses={
        401: {"description": "X-API-Key 헤더 누락"},
        403: {"description": "API Key 불일치"},
        429: {"description": "Rate Limit 초과 (분당 30회)"},
        503: {"description": "DART 기업 리스트 로딩 중"},
    },
)
@limiter.limit("30/minute")
def search_company(
    request: Request,
    keyword: str = KEYWORD_QUERY,
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    keyword = _normalize_keyword(keyword)
    try:
        corps = DartService.search_companies(keyword)
    except ServiceNotReadyError as e:
        raise HTTPException(status_code=503, detail=str(e))

    stock_codes = [c.stock_code for c in corps if c.stock_code]
    collected_codes: set[str] = set()
    if stock_codes:
        completed_codes = {
            row[0]
            for row in (
                db.query(CollectionTask.stock_code)
                .filter(
                    CollectionTask.stock_code.in_(stock_codes),
                    CollectionTask.status == "completed",
                )
                .all()
            )
        }
        data_codes = {
            row[0]
            for row in (
                db.query(FinancialStatement.company_id)
                .filter(FinancialStatement.company_id.in_(completed_codes))
                .distinct()
                .all()
            )
        }
        collected_codes = completed_codes & data_codes

    return [
        CompanySearchItem(
            company_name=c.corp_name,
            stock_code=c.stock_code,
            sector=DartService._get_corp_sector(c),
            is_collected=c.stock_code in collected_codes,
        )
        for c in corps
    ]

@router.get(
    "/collect/status",
    response_model=CollectStatusResponse,
    summary="수집 상태 조회",
    description=(
        "종목의 재무 데이터 수집 진행 상태를 반환합니다.\n\n"
        "| status | 의미 |\n"
        "|--------|------|\n"
        "| `none` | 수집 이력 없음 |\n"
        "| `processing` | 수집 진행 중 |\n"
        "| `completed` | 수집 완료 |\n"
        "| `failed` | 수집 실패 (`message`에 원인 포함) |\n\n"
        "`updated_at`은 UTC 기준 ISO 8601 형식입니다."
    ),
    responses={
        401: {"description": "X-API-Key 헤더 누락"},
        403: {"description": "API Key 불일치"},
        404: {"description": "해당 기업을 찾을 수 없음"},
        429: {"description": "Rate Limit 초과 (분당 60회)"},
        503: {"description": "DART 기업 리스트 로딩 중"},
    },
)
@limiter.limit("60/minute")
def check_status(
    request: Request,
    keyword: str = KEYWORD_QUERY,
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    actual_code = get_resolved_code(keyword)
    task = db.query(CollectionTask).filter(CollectionTask.stock_code == actual_code).first()
    if not task:
        return {"status": "none"}
    return {
        "status": task.status,
        "message": task.message,
        "started_at": task.started_at,
        "updated_at": task.updated_at,
    }


@router.get(
    "/collect/status/batch",
    response_model=List[BatchStatusItem],
    summary="수집 상태 일괄 조회",
    description=(
        "여러 종목의 수집 상태를 한 번에 조회합니다. (최대 10개)\n\n"
        "- 기업을 찾을 수 없거나 서비스 준비 중인 경우 해당 항목만 `error`로 반환합니다.\n"
        "- `/collect/batch`로 일괄 수집을 시작한 뒤 상태를 폴링할 때 사용하세요."
    ),
    responses={
        401: {"description": "X-API-Key 헤더 누락"},
        403: {"description": "API Key 불일치"},
        422: {"description": "keywords 1~10개 필요"},
        429: {"description": "Rate Limit 초과 (분당 30회)"},
    },
)
@limiter.limit("30/minute")
def check_status_batch(
    request: Request,
    keywords: List[str] = Query(..., description="조회할 기업명 또는 종목코드 (1~10개)"),
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    if len(keywords) > 10:
        raise HTTPException(status_code=422, detail="한 번에 최대 10개 종목까지 조회 가능합니다.")

    results: List[BatchStatusItem] = []
    for keyword in keywords:
        keyword, validation_error = _validate_keyword_item(keyword)
        if validation_error:
            results.append(BatchStatusItem(keyword=keyword, status="error", detail=validation_error))
            continue
        try:
            actual_code = get_resolved_code(keyword)
        except HTTPException as e:
            results.append(BatchStatusItem(keyword=keyword, status="error", detail=e.detail))
            continue

        task = db.query(CollectionTask).filter(CollectionTask.stock_code == actual_code).first()
        if not task:
            results.append(BatchStatusItem(keyword=keyword, resolved_code=actual_code, status="none"))
        else:
            results.append(BatchStatusItem(
                keyword=keyword,
                resolved_code=actual_code,
                status=task.status,
                message=task.message,
                started_at=task.started_at,
                updated_at=task.updated_at,
            ))
    return results


@router.post(
    "/collect/refresh",
    response_model=CollectRefreshResponse,
    summary="재무 데이터 강제 갱신",
    description=(
        "이미 수집된 종목의 재무 데이터를 최신 DART 공시로 다시 수집합니다.\n\n"
        "- 수집 중(`processing`)인 경우에만 차단됩니다.\n"
        "- 갱신 성공 시 기존 데이터를 새 데이터로 교체합니다. 실패 시 기존 데이터는 보존됩니다.\n"
        "- 완료 여부는 `/collect/status`로 폴링하세요."
    ),
    responses={
        401: {"description": "X-API-Key 헤더 누락"},
        403: {"description": "API Key 불일치"},
        404: {"description": "해당 기업을 찾을 수 없음"},
        422: {"description": "keyword 형식 오류"},
        429: {"description": "Rate Limit 초과 (분당 3회)"},
        503: {"description": "DART 기업 리스트 로딩 중"},
    },
)
@limiter.limit("3/minute")
def refresh_collect(
    request: Request,
    background_tasks: BackgroundTasks,
    keyword: str = KEYWORD_QUERY,
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    actual_code = get_resolved_code(keyword)
    if not _claim_collection_task(actual_code, db):
        return {"status": "already_processing", "resolved_code": actual_code}

    user_email = current_user.email if current_user else None
    background_tasks.add_task(_collect_with_email, actual_code, user_email)
    return {"status": "refreshing", "resolved_code": actual_code}


@router.post(
    "/collect",
    response_model=CollectStartResponse,
    summary="재무 데이터 수집 시작",
    description=(
        "지정한 종목의 재무 데이터를 DART에서 백그라운드로 수집합니다.\n\n"
        "- 수집은 비동기로 실행되며, 완료 여부는 `/collect/status`로 폴링하세요.\n"
        "- 이미 수집 중인 경우 `already_processing`을 반환하고 중복 수집을 차단합니다.\n"
        "- 최근 5개 사업연도 데이터를 수집합니다.\n"
        "- 금융업(은행·보험·증권)은 공통 지표만 수집됩니다."
    ),
    responses={
        401: {"description": "X-API-Key 헤더 누락"},
        403: {"description": "API Key 불일치"},
        404: {"description": "해당 기업을 찾을 수 없음"},
        422: {"description": "keyword 형식 오류 (길이 또는 허용 문자 위반)"},
        429: {"description": "Rate Limit 초과 (분당 5회)"},
        503: {"description": "DART 기업 리스트 로딩 중"},
    },
)
@limiter.limit("5/minute")
def collect(
    request: Request,
    background_tasks: BackgroundTasks,
    keyword: str = KEYWORD_QUERY,
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    actual_code = get_resolved_code(keyword)

    if _is_completed_with_financial_data(actual_code, db):
        return {"status": "already_processing", "resolved_code": actual_code}

    if not _claim_collection_task(actual_code, db):
        return {"status": "already_processing", "resolved_code": actual_code}

    user_email = current_user.email if current_user else None
    background_tasks.add_task(_collect_with_email, actual_code, user_email)
    return {"status": "started", "resolved_code": actual_code}


@router.get(
    "/summary/ai-analysis",
    response_model=AIAnalysisResponse,
    summary="AI 예측 + 자연어 분석",
    description=(
        "LightGBM 모델로 다음해 영업이익을 예측하고, GPT가 초보자용 분석을 생성합니다.\n\n"
        "- 입력: `keyword` (기업명 또는 6자리 종목코드)\n"
        "- 모델 파일(`models/model_nonfin.pkl`·`models/model_fin.pkl`) 또는 `OPENAI_API_KEY` 미설정 시 503\n"
        "- 5년치 데이터 부족 시 422"
    ),
    responses={
        404: {"description": "수집 데이터 없음"},
        422: {"description": "5년치 데이터 부족"},
        429: {"description": "Rate Limit 초과 (분당 20회)"},
        503: {"description": "모델 파일 또는 OPENAI_API_KEY 미설정, 외부 호출 실패"},
    },
)
@limiter.limit("20/minute")
def get_ai_analysis(
    request: Request,
    keyword: str = KEYWORD_QUERY,
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    actual_code = get_resolved_code(keyword)
    records = (
        db.query(FinancialStatement)
        .filter(FinancialStatement.company_id == actual_code)
        .order_by(FinancialStatement.fiscal_year.desc())
        .limit(5)
        .all()
    )
    if not records:
        raise HTTPException(status_code=404, detail="데이터 수집이 필요합니다.")
    if len(records) < 5:
        raise HTTPException(status_code=422, detail="예측에는 최근 5년치 데이터가 필요합니다.")
    is_financial = any("[금융업" in (r.source_report or "") for r in records)
    prediction_label = "당기순이익" if is_financial else "영업이익"

    latest_fy = records[0].fiscal_year
    base_year = latest_fy
    forecast_year = base_year + 1
    try:
        if is_financial:
            data = ai_analysis_service.records_to_fin_input(records)
            features = create_features_fin(dict(data))
            pred = predict_net_income(features, base_equity=data["equity_1"])
        else:
            data = ai_analysis_service.records_to_ai_input(records)
            features = create_features(dict(data))
            pred = predict_op_profit(features, data["revenue_4"])
        if not math.isfinite(pred):
            raise ValueError(f"유한하지 않은 예측값: {pred}")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=f"예측 서비스 미준비: {e}")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"예측 실패: {e}")

    display_text = build_prediction_display_text(pred, is_financial)
    prediction_sentence = f"{forecast_year}년 예측 결과는 {display_text}입니다."
    now = datetime.now(timezone.utc)
    cached = (
        db.query(AIAnalysisCache)
        .filter(
            AIAnalysisCache.stock_code == actual_code,
            AIAnalysisCache.fiscal_year == latest_fy,
            AIAnalysisCache.expires_at > now,
        )
        .first()
    )
    if (
        cached
        # fiscal_year is latest_fy, so it identifies both base_year and forecast_year.
        and canonical_prediction(cached.prediction) == canonical_prediction(pred)
        and _summary_years_are_valid(cached.summary, base_year, forecast_year)
        and not _summary_prediction_violations(
            cached.summary, base_year, forecast_year, display_text
        )
    ):
        return AIAnalysisResponse(
            stock_code=actual_code,
            company_name=records[0].company_name or "",
            prediction=cached.prediction,
            base_year=base_year,
            forecast_year=forecast_year,
            summary=cached.summary,
            summary_available=True,
            is_financial=is_financial,
            prediction_label=prediction_label,
            prediction_display_text=display_text,
            prediction_sentence=prediction_sentence,
        )

    ai_analysis_service.check_daily_limit()

    latest = records[0]
    previous = records[1] if len(records) > 1 else None
    metrics = compute_metrics(latest, previous)
    points = compute_interpretation_points(records, metrics)

    try:
        summary_text = summarize(
            data,
            base_year=base_year,
            forecast_year=forecast_year,
            prediction_display_text=display_text,
            interpretation_points=points,
        )
    except Exception as e:
        logger.warning("AI 요약 생략(예측만 제공): %s", e)
        return AIAnalysisResponse(
            stock_code=actual_code,
            company_name=records[0].company_name or "",
            prediction=pred,
            base_year=base_year,
            forecast_year=forecast_year,
            summary=None,
            summary_available=False,
            is_financial=is_financial,
            prediction_label=prediction_label,
            prediction_display_text=display_text,
            prediction_sentence=prediction_sentence,
        )

    year_valid = _summary_years_are_valid(summary_text, base_year, forecast_year)
    violations = _summary_prediction_violations(
        summary_text, base_year, forecast_year, display_text,
    )
    if not year_valid or violations:
        years = sorted({int(match[:4]) for match in _SUMMARY_YEAR_PATTERN.findall(summary_text)})
        logger.warning(
            "AI 요약 사실성 위반 [%s]: %s",
            actual_code,
            years if not year_valid else violations,
        )
        ai_analysis_service.increment_daily_counter()
        return AIAnalysisResponse(
            stock_code=actual_code,
            company_name=records[0].company_name or "",
            prediction=pred,
            base_year=base_year,
            forecast_year=forecast_year,
            summary=None,
            summary_available=False,
            is_financial=is_financial,
            prediction_label=prediction_label,
            prediction_display_text=display_text,
            prediction_sentence=prediction_sentence,
        )

    ai_analysis_service.upsert_cache(db, actual_code, latest_fy, pred, summary_text, now)
    ai_analysis_service.increment_daily_counter()

    return AIAnalysisResponse(
        stock_code=actual_code,
        company_name=records[0].company_name or "",
        prediction=pred,
        base_year=base_year,
        forecast_year=forecast_year,
        summary=summary_text,
        summary_available=True,
        is_financial=is_financial,
        prediction_label=prediction_label,
        prediction_display_text=display_text,
        prediction_sentence=prediction_sentence,
    )


@router.get(
    "/summary/trend",
    response_model=TrendResponse,
    summary="연도별 재무 트렌드 조회",
    description=(
        "차트용 연도별 주요 재무 지표를 반환합니다. "
        "`history` 배열은 오래된 연도부터 최신 순으로 정렬합니다.\n\n"
        "- 금액 필드: `unit=원` 또는 `unit=억` 선택 가능\n"
        "- 비율 필드: `operating_margin`, `net_margin`, `revenue_growth_rate` (단위: %)\n\n"
        "> 수집 전이면 404가 반환됩니다. 먼저 `/collect`를 호출하세요."
    ),
    responses={
        404: {"description": "수집 데이터 없음"},
        429: {"description": "Rate Limit 초과 (분당 30회)"},
        503: {"description": "DART 기업 리스트 로딩 중"},
    },
)
@limiter.limit("30/minute")
def get_summary_trend(
    request: Request,
    keyword: str = KEYWORD_QUERY,
    unit: str = Query(default="원", pattern="^(원|억)$", description="금액 단위 (원 | 억)"),
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    actual_code = get_resolved_code(keyword)
    records = (
        db.query(FinancialStatement)
        .filter(FinancialStatement.company_id == actual_code)
        .order_by(FinancialStatement.fiscal_year.desc())
        .all()
    )
    if not records:
        raise HTTPException(status_code=404, detail="데이터 수집이 필요합니다.")

    unit_divisor = 100_000_000 if unit == "억" else 1
    summary = _build_company_summary(actual_code, records, unit_divisor, db=db)

    return TrendResponse(
        stock_code=actual_code,
        company_name=summary.company_name,
        unit=unit,
        history=list(reversed(summary.history)),
    )


@router.get(
    "/summary/detail",
    response_model=CompanyDetailResponse,
    summary="종목 상세 통합 조회",
    description=(
        "종목 상세 화면용 통합 응답. summary + trend + 수집상태를 한 번에 반환합니다.\n\n"
        "- 프론트 측 `/summary`, `/summary/trend`, `/collect/status` 3회 호출을 1회로 대체.\n"
        "- `unit` 파라미터는 `summary`/`trend` 의 금액 단위에 모두 적용됩니다.\n"
        "- AI 분석은 별도 엔드포인트(`/summary/ai-analysis`)로 분리됨."
    ),
    responses={
        401: {"description": "X-API-Key 헤더 누락"},
        403: {"description": "API Key 불일치"},
        404: {"description": "수집 데이터 없음 — 먼저 /collect 호출 필요"},
        422: {"description": "keyword 형식 오류"},
        429: {"description": "Rate Limit 초과 (분당 20회)"},
        503: {"description": "DART 기업 리스트 로딩 중"},
    },
)
@limiter.limit("20/minute")
def get_summary_detail(
    request: Request,
    background_tasks: BackgroundTasks,
    keyword: str = KEYWORD_QUERY,
    unit: str = Query(default="원", pattern="^(원|억)$", description="금액 단위 (원 | 억)"),
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    actual_code = get_resolved_code(keyword)
    records = (
        db.query(FinancialStatement)
        .filter(FinancialStatement.company_id == actual_code)
        .order_by(FinancialStatement.fiscal_year.desc())
        .all()
    )
    if not records:
        raise HTTPException(status_code=404, detail="데이터 수집이 필요합니다.")

    unit_divisor = 100_000_000 if unit == "억" else 1
    summary = _build_company_summary(actual_code, records, unit_divisor, db=db)
    trend = TrendResponse(
        stock_code=actual_code,
        company_name=summary.company_name,
        unit=unit,
        history=list(reversed(summary.history)),
    )
    task = db.query(CollectionTask).filter(CollectionTask.stock_code == actual_code).first()
    background_tasks.add_task(
        _write_search_log, actual_code, summary.company_name,
        current_user.id if current_user else None,
    )

    return CompanyDetailResponse(
        summary=summary,
        trend=trend,
        collection_status=task.status if task else None,
        collection_updated_at=task.updated_at if task else None,
    )


@router.get(
    "/similar",
    response_model=List[SimilarCompanyItem],
    summary="유사 기업 추천",
    description=(
        "기준 종목과 같은 섹터 · 매출 규모 ±50% · 성장성/안정성/수익성 중 2개 이상 일치하는 기업을 "
        "매출 근접도 순으로 반환합니다.\n\n"
        "- 결과는 매출 근접도 오름차순(가장 비슷한 규모부터).\n"
        "- 기준 종목 자신은 결과에서 제외.\n"
        "- 결과 0건이면 빈 배열."
    ),
    responses={
        401: {"description": "X-API-Key 헤더 누락"},
        404: {"description": "기준 종목 데이터 없음"},
        422: {"description": "keyword 형식 오류"},
        429: {"description": "Rate Limit 초과 (분당 30회)"},
        503: {"description": "DART 기업 리스트 로딩 중"},
    },
)
@limiter.limit("30/minute")
def get_similar_companies(
    request: Request,
    keyword: str = KEYWORD_QUERY,
    limit: int = Query(default=5, ge=1, le=20),
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    actual_code = get_resolved_code(keyword)
    cache_key = (actual_code, limit)
    now = datetime.now(timezone.utc)
    cached = _similar_cache.get(cache_key)
    if cached and cached["expires_at"] > now:
        return cached["value"]

    ref_records = (
        db.query(FinancialStatement)
        .filter(FinancialStatement.company_id == actual_code)
        .order_by(FinancialStatement.fiscal_year.desc())
        .limit(2)
        .all()
    )
    if not ref_records:
        raise HTTPException(status_code=404, detail="기준 종목 데이터가 없습니다.")

    ref_latest = ref_records[0]
    ref_previous = ref_records[1] if len(ref_records) > 1 else None
    ref_revenue = ref_latest.revenue or 0
    if ref_revenue <= 0:
        return []

    ref_sector = _safe_get_sector(actual_code)
    if not ref_sector:
        return []

    ref_growth = evaluate_growth(ref_latest, ref_previous)
    ref_stability = evaluate_stability(ref_latest)
    ref_profitability = evaluate_profitability(ref_latest, ref_previous)
    candidate_groups = _fetch_latest_two_per_company(db)
    candidate_groups.pop(actual_code, None)

    low = ref_revenue * 0.5
    high = ref_revenue * 1.5
    results: List[tuple[int, SimilarCompanyItem]] = []

    for company_id, records in candidate_groups.items():
        latest = records[0]
        previous = records[1] if len(records) > 1 else None
        candidate_revenue = latest.revenue or 0
        if not (low <= candidate_revenue <= high):
            continue

        candidate_sector = _safe_get_sector(company_id)
        if candidate_sector != ref_sector:
            continue

        candidate_growth = evaluate_growth(latest, previous)
        candidate_stability = evaluate_stability(latest)
        candidate_profitability = evaluate_profitability(latest, previous)
        match_count = sum([
            candidate_growth == ref_growth and candidate_growth != "데이터 부족",
            candidate_stability == ref_stability and candidate_stability != "데이터 부족",
            candidate_profitability == ref_profitability and candidate_profitability != "데이터 부족",
        ])
        if match_count < 2:
            continue

        operating_margin = None
        if latest.revenue and latest.operating_profit is not None:
            operating_margin = round(latest.operating_profit / latest.revenue * 100, 2)

        results.append((
            abs(candidate_revenue - ref_revenue),
            SimilarCompanyItem(
                stock_code=company_id,
                company_name=latest.company_name or "",
                sector=candidate_sector,
                fiscal_year=latest.fiscal_year,
                revenue=latest.revenue,
                operating_profit=latest.operating_profit,
                operating_margin=operating_margin,
                growth_status=candidate_growth,
                profitability_status=candidate_profitability,
                stability_status=candidate_stability,
                match_count=match_count,
            ),
        ))

    results.sort(key=lambda item: item[0])
    final = [item for _, item in results[:limit]]
    _similar_cache[cache_key] = {
        "value": final,
        "expires_at": now + timedelta(seconds=_SIMILAR_CACHE_TTL_SECONDS),
    }
    return final


@router.get(
    "/recommendation",
    response_model=List[RecommendedCompanyItem],
    summary="우량주 큐레이션",
    description=(
        "성장성/안정성/수익성 중 2개 이상 양호하며 최근 90일 내 수집된 기업을 영업이익률 desc 순으로 반환합니다.\n\n"
        "- 양호 기준: 성장성=`양호` / 안정성=`우수` / 수익성=`개선`.\n"
        "- 섹터 다양성: 같은 섹터는 최대 2개.\n"
        "- 결과 0건이면 빈 배열."
    ),
    responses={
        401: {"description": "X-API-Key 헤더 누락"},
        429: {"description": "Rate Limit 초과 (분당 30회)"},
    },
)
@limiter.limit("30/minute")
def get_recommendation(
    request: Request,
    limit: int = Query(default=5, ge=1, le=20),
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    now = datetime.now(timezone.utc)
    cached = _recommendation_cache.get(limit)
    if cached and cached["expires_at"] > now:
        return cached["value"]

    result = _build_recommendation_items(db, limit, now=now)
    _recommendation_cache[limit] = {
        "value": result,
        "expires_at": now + timedelta(seconds=_RECOMMENDATION_CACHE_TTL_SECONDS),
    }
    return result


@router.get(
    "/summary",
    response_model=CompanySummary,
    summary="재무 분석 요약 조회",
    description=(
        "수집된 재무 데이터를 기반으로 성장성·안정성·수익성 분석 결과를 반환합니다.\n\n"
        "**분석 지표**\n"
        "- `growth_status`: 전년 대비 매출 증가 여부 (`양호` / `부진` / `데이터 부족`)\n"
        "- `stability_status`: 부채비율 100% 이하 여부 (`우수` / `주의` / `데이터 부족`)\n"
        "- `profitability_status`: 전년 대비 영업이익률 개선 여부 (`개선` / `유지` / `데이터 부족`)\n\n"
        "**`history`** 배열은 최신 연도 순으로 정렬됩니다.\n\n"
        "- 금액 필드: `unit=원`(기본) 또는 `unit=억` 선택 가능\n"
        "- 비율 필드: `operating_margin`(영업이익률%), `net_margin`(순이익률%), `revenue_growth_rate`(매출 성장률%) — 차트 직접 사용 가능\n\n"
        "> 수집 전이면 404가 반환됩니다. 먼저 `/collect`를 호출하세요.\n\n"
        "> `sector_avg`는 동종업계 수집 완료 기업의 평균 지표입니다. "
        "`sector_avg.revenue_growth_rate`는 항상 `null`입니다(peer별 전년 데이터 비교 미적용)."
    ),
    responses={
        401: {"description": "X-API-Key 헤더 누락"},
        403: {"description": "API Key 불일치"},
        404: {"description": "수집 데이터 없음 — 먼저 /collect 호출 필요"},
        422: {"description": "keyword 형식 오류"},
        429: {"description": "Rate Limit 초과 (분당 30회)"},
        503: {"description": "DART 기업 리스트 로딩 중"},
    },
)
@limiter.limit("30/minute")
def get_summary(
    request: Request,
    background_tasks: BackgroundTasks,
    keyword: str = KEYWORD_QUERY,
    unit: str = Query(default="원", pattern="^(원|억)$", description="금액 단위 (원 | 억)"),
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    actual_code = get_resolved_code(keyword)
    records = (
        db.query(FinancialStatement)
        .filter(FinancialStatement.company_id == actual_code)
        .order_by(FinancialStatement.fiscal_year.desc())
        .all()
    )
    if not records:
        raise HTTPException(status_code=404, detail="데이터 수집이 필요합니다.")

    task = db.query(CollectionTask).filter(CollectionTask.stock_code == actual_code).first()
    if task and task.status == "completed" and task.updated_at:
        updated = task.updated_at if task.updated_at.tzinfo else task.updated_at.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - updated).days >= 7:
            background_tasks.add_task(_silent_refresh, actual_code)
            logger.info("TTL 만료 백그라운드 갱신 트리거: %s", actual_code)

    unit_divisor = 100_000_000 if unit == "억" else 1
    summary = _build_company_summary(actual_code, records, unit_divisor, db=db)
    background_tasks.add_task(
        _write_search_log, actual_code, summary.company_name,
        current_user.id if current_user else None,
    )
    return summary


@router.get(
    "/summary/export",
    summary="재무 분석 리포트 다운로드 (PDF / CSV)",
    description=(
        "수집된 재무 데이터를 PDF 또는 CSV로 다운로드합니다.\n\n"
        "- `format=pdf` (기본): 연도별 재무 데이터 표, 분석 결과 포함\n"
        "- `format=csv`: 연도별 원시 재무 데이터 (스프레드시트 분석용)\n"
        "> 수집 전이면 404가 반환됩니다. 먼저 `/collect`를 호출하세요."
    ),
    responses={
        200: {"content": {"application/pdf": {}, "text/csv": {}}, "description": "파일"},
        401: {"description": "X-API-Key 헤더 누락"},
        403: {"description": "API Key 불일치"},
        404: {"description": "수집 데이터 없음"},
        429: {"description": "Rate Limit 초과 (분당 10회)"},
        503: {"description": "DART 기업 리스트 로딩 중"},
    },
)
@limiter.limit("10/minute")
def export_summary(
    request: Request,
    keyword: str = KEYWORD_QUERY,
    format: str = Query(default="pdf", pattern="^(pdf|csv)$", description="출력 형식 (pdf | csv)"),
    unit: str = Query(default="원", pattern="^(원|억)$", description="금액 단위 (원 | 억) — CSV 전용"),
    include_ai: bool = Query(
        default=False,
        description="True 면 캐시된 AI 분석(예측·요약)을 리포트 끝에 추가. 캐시 없으면 조용히 생략.",
    ),
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    actual_code = get_resolved_code(keyword)
    records = (
        db.query(FinancialStatement)
        .filter(FinancialStatement.company_id == actual_code)
        .order_by(FinancialStatement.fiscal_year.desc())
        .all()
    )
    if not records:
        raise HTTPException(status_code=404, detail="데이터 수집이 필요합니다.")

    # db 미전달: export(PDF/CSV)에는 sector_avg 불포함.
    unit_divisor = 100_000_000 if unit == "억" and format == "csv" else 1
    summary = _build_company_summary(actual_code, records, unit_divisor=unit_divisor)
    ai_cache = None
    if include_ai:
        now = datetime.now(timezone.utc)
        ai_cache = (
            db.query(AIAnalysisCache)
            .filter(
                AIAnalysisCache.stock_code == actual_code,
                AIAnalysisCache.fiscal_year == records[0].fiscal_year,
                AIAnalysisCache.expires_at > now,
            )
            .first()
        )
        if ai_cache is not None:
            is_financial = any("[금융업" in (r.source_report or "") for r in records)
            base_year = records[0].fiscal_year
            forecast_year = base_year + 1
            display_text = build_prediction_display_text(ai_cache.prediction, is_financial)
            if not (
                _summary_years_are_valid(ai_cache.summary, base_year, forecast_year)
                and not _summary_prediction_violations(
                    ai_cache.summary, base_year, forecast_year, display_text
                )
            ):
                ai_cache = None
    from urllib.parse import quote

    if format == "csv":
        import csv, io
        buf = io.StringIO()
        writer = csv.writer(buf)
        unit_label = "억원" if unit == "억" else "원"
        writer.writerow([
            "기업명", "종목코드", "사업연도",
            f"매출액({unit_label})", f"매출원가({unit_label})",
            f"매출총이익({unit_label})", f"판관비({unit_label})",
            f"영업이익({unit_label})", f"당기순이익({unit_label})",
            f"총자산({unit_label})", f"총부채({unit_label})",
            f"자기자본({unit_label})", f"현금({unit_label})", "출처",
        ])
        for yr in summary.history:
            writer.writerow([
                summary.company_name, summary.stock_code, yr.fiscal_year,
                yr.revenue, yr.cost_of_sales, yr.gross_profit, yr.sga,
                yr.operating_profit, yr.net_income, yr.total_assets,
                yr.total_liabilities, yr.equity, yr.cash, yr.source_report,
            ])
        if ai_cache is not None:
            writer.writerow([])
            writer.writerow(["AI 분석"])
            writer.writerow(["다음해 예측 영업이익(원)", ai_cache.prediction])
            writer.writerow(["요약", ai_cache.summary])
        filename_encoded = quote(f"{summary.company_name}_{summary.fiscal_year}_재무분석.csv")
        return Response(
            content=buf.getvalue().encode("utf-8-sig"),  # utf-8-sig: Excel BOM
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename_encoded}"},
        )

    pdf_bytes = build_summary_pdf(summary, ai_cache=ai_cache)
    filename_encoded = quote(f"{summary.company_name}_{summary.fiscal_year}_재무분석.pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename_encoded}"},
    )


# ──────────────────────────────────────────────
# GET /companies — 수집 완료 기업 목록
# ──────────────────────────────────────────────

@router.get(
    "/companies",
    response_model=List[CollectedCompany],
    summary="수집 완료 기업 목록 조회",
    description=(
        "재무 데이터가 수집 완료된 기업 목록을 반환합니다.\n\n"
        "각 기업의 최신 사업연도와 수집 완료 시각이 포함됩니다."
    ),
    responses={
        401: {"description": "X-API-Key 헤더 누락"},
        403: {"description": "API Key 불일치"},
        429: {"description": "Rate Limit 초과 (분당 30회)"},
    },
)
@limiter.limit("30/minute")
def list_companies(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200, description="반환할 최대 기업 수"),
    offset: int = Query(default=0, ge=0, description="건너뛸 기업 수 (페이지네이션)"),
    sector: Optional[List[str]] = Query(default=None, description="섹터 필터 (복수 가능, 미설정 시 전체)"),
    growth_status: Optional[str] = Query(default=None, description="성장성 필터 (양호|부진|데이터 부족)"),
    stability_status: Optional[str] = Query(default=None, description="안정성 필터 (우수|주의|데이터 부족)"),
    min_revenue: Optional[int] = Query(default=None, ge=0, description="Minimum revenue"),
    min_operating_margin: Optional[float] = Query(default=None, description="Minimum operating margin (%)"),
    max_debt_ratio: Optional[float] = Query(default=None, ge=0, description="Maximum debt ratio (%)"),
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    # 서브쿼리로 company_id별 최신 fiscal_year를 한 번에 가져온 뒤 JOIN
    # → N+1 쿼리 대신 쿼리 2번으로 처리 (completed 목록 + 최신연도 서브쿼리)
    latest_year_sq = (
        db.query(
            FinancialStatement.company_id,
            func.max(FinancialStatement.fiscal_year).label("max_year"),
        )
        .group_by(FinancialStatement.company_id)
        .subquery()
    )

    query = (
        db.query(FinancialStatement, CollectionTask.updated_at)
        .join(
            latest_year_sq,
            (FinancialStatement.company_id == latest_year_sq.c.company_id)
            & (FinancialStatement.fiscal_year == latest_year_sq.c.max_year),
        )
        .join(
            CollectionTask,
            (CollectionTask.stock_code == FinancialStatement.company_id)
            & (CollectionTask.status == "completed"),
        )
        .order_by(FinancialStatement.company_name)
    )

    need_filter = bool(
        sector or growth_status or stability_status
        or min_revenue is not None
        or min_operating_margin is not None
        or max_debt_ratio is not None
    )

    if not need_filter:
        total = query.count()
        rows = query.offset(offset).limit(limit).all()
    else:
        all_rows = query.all()

        prev_by_code: dict = {}
        if growth_status:
            codes = [fs.company_id for fs, _ in all_rows]
            if codes:
                all_fs = (
                    db.query(FinancialStatement)
                    .filter(FinancialStatement.company_id.in_(codes))
                    .order_by(
                        FinancialStatement.company_id,
                        FinancialStatement.fiscal_year.desc(),
                    )
                    .all()
                )
                for rec in all_fs:
                    bucket = prev_by_code.setdefault(rec.company_id, [])
                    if len(bucket) < 2:
                        bucket.append(rec)

        sector_set = set(sector) if sector else None
        filtered = []
        for fs, updated_at in all_rows:
            if sector_set and DartService.get_sector_by_stock_code(fs.company_id) not in sector_set:
                continue
            if growth_status:
                recs = prev_by_code.get(fs.company_id, [fs])
                latest = recs[0]
                previous = recs[1] if len(recs) > 1 else None
                if evaluate_growth(latest, previous) != growth_status:
                    continue
            if stability_status and evaluate_stability(fs) != stability_status:
                continue
            if min_revenue is not None and (fs.revenue is None or fs.revenue < min_revenue):
                continue
            if min_operating_margin is not None:
                if fs.operating_profit is None or not fs.revenue:
                    continue
                if fs.operating_profit / fs.revenue * 100 < min_operating_margin:
                    continue
            if max_debt_ratio is not None:
                if fs.total_liabilities is None or not fs.equity or fs.equity <= 0:
                    continue
                if fs.total_liabilities / fs.equity * 100 > max_debt_ratio:
                    continue
            filtered.append((fs, updated_at))

        total = len(filtered)
        rows = filtered[offset: offset + limit]

    items = [
        CollectedCompany(
            company_name=fs.company_name or fs.company_id,
            stock_code=fs.company_id,
            fiscal_year=fs.fiscal_year,
            sector=DartService.get_sector_by_stock_code(fs.company_id),
            collected_at=updated_at,
        )
        for fs, updated_at in rows
    ]
    return JSONResponse(
        content=TypeAdapter(List[CollectedCompany]).dump_python(items, mode="json"),
        headers={"X-Total-Count": str(total)},
    )


# ──────────────────────────────────────────────
# GET /compare — 복수 기업 재무 비교
# ──────────────────────────────────────────────

def _build_compare_response(
    keywords: List[str],
    db: Session,
    user_id: Optional[int] = None,
    background_tasks: Optional[BackgroundTasks] = None,
    unit_divisor: int = 1,
) -> CompareResponse:
    """키워드 목록으로 CompareResponse 생성 및 SearchLog 기록. /compare, /compare/export 공용."""
    if len(keywords) < 2:
        raise HTTPException(status_code=422, detail="비교할 기업을 2개 이상 입력하세요.")
    if len(keywords) > 5:
        raise HTTPException(status_code=422, detail="한 번에 최대 5개 기업까지 비교 가능합니다.")
    keywords = _validate_keywords(keywords, max_count=5)

    resolved = [(kw, get_resolved_code(kw)) for kw in keywords]
    actual_codes = [code for _, code in resolved]

    all_records = (
        db.query(FinancialStatement)
        .filter(FinancialStatement.company_id.in_(actual_codes))
        .order_by(FinancialStatement.fiscal_year.desc())
        .all()
    )

    records_by_code: dict[str, list] = defaultdict(list)
    for r in all_records:
        records_by_code[r.company_id].append(r)

    items: List[CompanyCompareItem] = []
    for keyword, actual_code in resolved:
        records = records_by_code.get(actual_code, [])
        if not records:
            raise HTTPException(
                status_code=404,
                detail=f"'{keyword}' ({actual_code}) 데이터 수집이 필요합니다. /collect를 먼저 호출하세요.",
            )
        latest = records[0]
        previous = records[1] if len(records) > 1 else None
        metrics = compute_metrics(latest, previous)
        history = _enrich_history(records, unit_divisor)
        items.append(
            CompanyCompareItem(
                company_name=latest.company_name or actual_code,
                stock_code=actual_code,
                fiscal_year=latest.fiscal_year,
                sector=DartService.get_sector_by_stock_code(actual_code),
                is_financial_sector="[금융업" in (latest.source_report or ""),
                metrics=metrics,
                history=history,
            )
        )
        if background_tasks:
            background_tasks.add_task(
                _write_search_log, actual_code, latest.company_name or actual_code, user_id,
            )

    return CompareResponse(companies=items, compared_at=datetime.now(timezone.utc))


_COMPARE_KEYWORDS_QUERY = Query(
    ...,
    description="비교할 기업명 또는 종목코드 (2~5개)",
    alias="keywords",
)

_COMPARE_RESPONSES = {
    401: {"description": "X-API-Key 헤더 누락"},
    403: {"description": "API Key 불일치"},
    404: {"description": "지정 기업 중 수집 데이터 없음"},
    422: {"description": "keywords 2~5개 필요"},
    429: {"description": "Rate Limit 초과 (분당 20회)"},
    503: {"description": "DART 기업 리스트 로딩 중"},
}


_COMPARE_INSIGHT_RESPONSES = {
    **_COMPARE_RESPONSES,
    429: {"description": "Rate Limit 초과 (분당 5회)"},
}


_EXPORT_COMPARE_RESPONSES = {
    **_COMPARE_RESPONSES,
    429: {"description": "Rate Limit 초과 (분당 5회)"},
}


@router.get(
    "/compare",
    response_model=CompareResponse,
    summary="복수 기업 재무 비교",
    description=(
        "최대 5개 기업의 최신 재무 지표를 나란히 반환합니다.\n\n"
        "**계산 지표**\n"
        "| 지표 | 설명 |\n"
        "|------|------|\n"
        "| `revenue_growth_rate` | 전년 대비 매출 성장률(%) |\n"
        "| `operating_margin` | 영업이익률(%) |\n"
        "| `net_margin` | 순이익률(%) |\n"
        "| `roe` | 자기자본이익률(%) |\n"
        "| `roa` | 총자산이익률(%) |\n"
        "| `debt_ratio` | 부채비율(%) |\n\n"
        "전년 데이터가 없는 경우 `revenue_growth_rate`는 `null`로 반환됩니다.\n\n"
        "> 수집되지 않은 기업이 포함된 경우 해당 기업만 404 오류를 반환합니다."
    ),
    responses=_COMPARE_RESPONSES,
)
@limiter.limit("20/minute")
def compare_companies(
    request: Request,
    background_tasks: BackgroundTasks,
    keywords: List[str] = _COMPARE_KEYWORDS_QUERY,
    unit: str = Query(default="원", pattern="^(원|억)$", description="금액 단위 (원 | 억)"),
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    unit_divisor = 100_000_000 if unit == "억" else 1
    return _build_compare_response(
        keywords,
        db,
        user_id=current_user.id if current_user else None,
        background_tasks=background_tasks,
        unit_divisor=unit_divisor,
    )


@router.get(
    "/compare/ai-insight",
    response_model=CompareInsightResponse,
    summary="복수 기업 AI 비교 인사이트",
    responses=_COMPARE_INSIGHT_RESPONSES,
)
@limiter.limit("5/minute")
def compare_ai_insight(
    request: Request,
    background_tasks: BackgroundTasks,
    keywords: List[str] = _COMPARE_KEYWORDS_QUERY,
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    base = _build_compare_response(
        keywords,
        db,
        user_id=current_user.id if current_user else None,
        background_tasks=background_tasks,
    )
    payload = [
        {
            "company_name": company.company_name,
            "stock_code": company.stock_code,
            "fiscal_year": company.fiscal_year,
            "metrics": (
                company.metrics.model_dump()
                if hasattr(company.metrics, "model_dump")
                else dict(company.metrics)
            ),
        }
        for company in base.companies
    ]

    ai_analysis_service.check_daily_limit()
    try:
        insight = summarize_compare(payload)
    except Exception as e:
        logger.warning("Compare AI insight skipped: %s", e)
        return CompareInsightResponse(
            companies=base.companies,
            insight=None,
            insight_available=False,
            compared_at=base.compared_at,
        )
    ai_analysis_service.increment_daily_counter()
    return CompareInsightResponse(
        companies=base.companies,
        insight=insight,
        insight_available=True,
        compared_at=base.compared_at,
    )


@router.get(
    "/compare/export",
    summary="복수 기업 비교 리포트 다운로드 (PDF / CSV)",
    description=(
        "최대 5개 기업의 핵심 재무 지표 비교 및 연도별 재무 데이터를 PDF 또는 CSV로 반환합니다.\n\n"
        "- `format=pdf` (기본): 지표 비교 표 + 기업별 연도별 재무 데이터\n"
        "- `format=csv`: 기업별·연도별 원시 재무 데이터\n"
        "> 수집되지 않은 기업이 포함된 경우 404가 반환됩니다."
    ),
    responses={
        200: {"content": {"application/pdf": {}, "text/csv": {}}, "description": "파일"},
        **_EXPORT_COMPARE_RESPONSES,
    },
)
@limiter.limit("5/minute")
def export_compare(
    request: Request,
    keywords: List[str] = _COMPARE_KEYWORDS_QUERY,
    format: str = Query(default="pdf", pattern="^(pdf|csv)$", description="출력 형식 (pdf | csv)"),
    unit: str = Query(default="원", pattern="^(원|억)$", description="금액 단위 (원 | 억)"),
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    from urllib.parse import quote
    unit_divisor = 100_000_000 if unit == "억" and format == "csv" else 1
    compare_result = _build_compare_response(
        keywords,
        db,
        user_id=current_user.id if current_user else None,
        unit_divisor=unit_divisor,
    )
    names = "_".join(c.company_name for c in compare_result.companies[:3])

    if format == "csv":
        import csv, io
        buf = io.StringIO()
        writer = csv.writer(buf)
        unit_label = "억원" if unit == "억" else "원"
        # sector는 재무 수치 중심 CSV에서 제외 — JSON 응답(/compare)에만 포함.
        writer.writerow([
            "기업명", "종목코드", "사업연도",
            "매출성장률(%)", "영업이익률(%)", "순이익률(%)",
            "ROE(%)", "ROA(%)", "부채비율(%)",
            f"매출액({unit_label})", f"영업이익({unit_label})", f"당기순이익({unit_label})",
            f"총자산({unit_label})", f"총부채({unit_label})", f"자기자본({unit_label})",
        ])
        for company in compare_result.companies:
            m = company.metrics
            for yr in company.history:
                is_latest = yr.fiscal_year == company.fiscal_year
                writer.writerow([
                    company.company_name, company.stock_code, yr.fiscal_year,
                    m.revenue_growth_rate if is_latest else "",
                    m.operating_margin if is_latest else "",
                    m.net_margin if is_latest else "",
                    m.roe if is_latest else "",
                    m.roa if is_latest else "",
                    m.debt_ratio if is_latest else "",
                    yr.revenue, yr.operating_profit, yr.net_income,
                    yr.total_assets, yr.total_liabilities, yr.equity,
                ])
        filename_encoded = quote(f"{names}_비교분석.csv")
        return Response(
            content=buf.getvalue().encode("utf-8-sig"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename_encoded}"},
        )

    pdf_bytes = build_compare_pdf(compare_result)
    filename_encoded = quote(f"{names}_비교분석.pdf")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename_encoded}"},
    )


@router.post(
    "/compare/save",
    response_model=CompareShareResponse,
    status_code=201,
    summary="비교 결과 영구 링크 생성",
    description=(
        "현재 keywords 조합으로 비교 결과를 계산해 저장하고 짧은 share_id를 반환합니다.\n\n"
        "- 익명 접근(URL 알면 누구나 조회).\n"
        "- 30일 후 자동 삭제.\n"
        "- 응답: `{share_id, expires_at}`.\n"
        "- 조회는 `GET /api/v1/finance/compare/{share_id}` 사용."
    ),
    responses={
        401: {"description": "X-API-Key 헤더 누락"},
        404: {"description": "비교 기업 일부를 찾을 수 없음"},
        422: {"description": "keywords 형식/개수 오류"},
        429: {"description": "Rate Limit 초과 (분당 5회)"},
    },
)
@limiter.limit("5/minute")
def save_compare(
    request: Request,
    background_tasks: BackgroundTasks,
    keywords: List[str] = _COMPARE_KEYWORDS_QUERY,
    unit: str = Query(default="원", pattern="^(원|억)$"),
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    import secrets

    unit_divisor = 100_000_000 if unit == "억" else 1
    response = _build_compare_response(
        keywords,
        db,
        user_id=current_user.id if current_user else None,
        background_tasks=background_tasks,
        unit_divisor=unit_divisor,
    )
    share_id = secrets.token_hex(6)
    expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    db.add(CompareShare(
        share_id=share_id,
        payload=response.model_dump_json(),
        expires_at=expires_at,
    ))
    db.commit()
    return CompareShareResponse(share_id=share_id, expires_at=expires_at)


@router.get(
    "/compare/{share_id}",
    response_model=CompareResponse,
    summary="저장된 비교 결과 조회",
    description=(
        "share_id 로 저장된 비교 결과(JSON)를 반환합니다.\n\n"
        "- 만료된 share_id 는 404. 만료 데이터는 서버 기동 시 자동 정리됨.\n"
        "- 익명 조회 가능."
    ),
    responses={
        404: {"description": "share_id 없음 또는 만료됨"},
        429: {"description": "Rate Limit 초과 (분당 60회)"},
    },
)
@limiter.limit("60/minute")
def get_compare_share(
    request: Request,
    share_id: str = Path(..., pattern=r"^[a-f0-9]{8,16}$"),
    db: Session = Depends(get_db),
):
    row = db.query(CompareShare).filter(CompareShare.share_id == share_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="공유 링크를 찾을 수 없습니다.")

    expires_at = row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=timezone.utc)
    if expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=404, detail="만료된 공유 링크입니다.")

    return CompareResponse(**json.loads(row.payload))


# ──────────────────────────────────────────────
# DELETE /companies/{stock_code} — 수집 데이터 삭제
# ──────────────────────────────────────────────

@router.delete(
    "/companies/{stock_code}",
    response_model=DeleteCompanyResponse,
    summary="수집된 기업 데이터 삭제",
    description=(
        "지정한 종목의 재무 데이터와 수집 태스크를 삭제합니다.\n\n"
        "**관리자 전용 API입니다. 관리자 화면에서만 호출하세요.**\n\n"
        "- 삭제 후 해당 종목은 `/companies` 목록에서 제거됩니다.\n"
        "- 재수집하려면 다시 `POST /collect`를 호출하세요."
    ),
    responses={
        401: {"description": "토큰 없음 또는 만료"},
        403: {"description": "관리자 권한 없음"},
        404: {"description": "수집 데이터 없음"},
        429: {"description": "Rate Limit 초과 (분당 10회)"},
    },
)
@limiter.limit("10/minute")
def delete_company(
    request: Request,
    stock_code: str = Path(..., pattern=r"^\d{6}$", description="6자리 숫자 종목코드"),
    db: Session = Depends(get_db),
    current_user=Depends(get_admin_user),
):
    task = db.query(CollectionTask).filter(CollectionTask.stock_code == stock_code).first()
    if not task:
        raise HTTPException(status_code=404, detail="수집된 데이터가 없습니다.")

    db.query(FinancialStatement).filter(FinancialStatement.company_id == stock_code).delete()
    db.query(AIAnalysisCache).filter(AIAnalysisCache.stock_code == stock_code).delete()
    db.delete(task)
    db.commit()
    return DeleteCompanyResponse(status="deleted", stock_code=stock_code)


# ──────────────────────────────────────────────
# POST /collect/batch — 복수 기업 일괄 수집
# ──────────────────────────────────────────────

@router.post(
    "/collect/batch",
    response_model=BatchCollectResponse,
    summary="복수 기업 재무 데이터 일괄 수집",
    description=(
        "2~10개 기업의 재무 데이터를 한 번에 백그라운드 수집 시작합니다.\n\n"
        "- 이미 수집 중인 기업은 `already_processing`으로 건너뜁니다.\n"
        "- 기업을 찾을 수 없거나 형식 오류인 경우 해당 항목만 `error`로 반환합니다.\n"
        "- 완료 여부는 각 `resolved_code`로 `/collect/status`를 폴링하세요."
    ),
    responses={
        401: {"description": "X-API-Key 헤더 누락"},
        403: {"description": "API Key 불일치"},
        422: {"description": "keywords 2~10개 필요"},
        429: {"description": "Rate Limit 초과 (분당 2회)"},
        503: {"description": "DART 기업 리스트 로딩 중"},
    },
)
@limiter.limit("2/minute")
def collect_batch(
    request: Request,
    background_tasks: BackgroundTasks,
    keywords: List[str] = Query(..., description="수집할 기업명 또는 종목코드 (2~10개)"),
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    if len(keywords) < 2:
        raise HTTPException(status_code=422, detail="2개 이상의 기업을 입력하세요.")
    if len(keywords) > 10:
        raise HTTPException(status_code=422, detail="한 번에 최대 10개 기업까지 수집 가능합니다.")

    results: List[BatchCollectItem] = []

    for keyword in keywords:
        keyword, validation_error = _validate_keyword_item(keyword)
        if validation_error:
            results.append(BatchCollectItem(keyword=keyword, status="error", detail=validation_error))
            continue
        try:
            actual_code = get_resolved_code(keyword)
        except HTTPException as e:
            results.append(BatchCollectItem(keyword=keyword, status="error", detail=e.detail))
            continue

        if _is_completed_with_financial_data(actual_code, db):
            results.append(BatchCollectItem(keyword=keyword, status="already_processing", resolved_code=actual_code))
            continue

        if not _claim_collection_task(actual_code, db):
            results.append(BatchCollectItem(keyword=keyword, status="already_processing", resolved_code=actual_code))
            continue

        background_tasks.add_task(_collect_with_email, actual_code, None)
        results.append(BatchCollectItem(keyword=keyword, status="started", resolved_code=actual_code))

    return BatchCollectResponse(results=results)


# ──────────────────────────────────────────────
# GET /popular — 인기 종목 (많이 조회된 기업)
# ──────────────────────────────────────────────

PERIOD_DAYS = {"today": 1, "week": 7, "month": 30, "all": None}

@router.get(
    "/popular",
    response_model=List[PopularCompany],
    summary="인기 종목 조회",
    description=(
        "많이 조회된 기업을 순위별로 반환합니다.\n\n"
        "| period | 기간 |\n"
        "|--------|------|\n"
        "| `today` | 오늘 |\n"
        "| `week` | 최근 7일 |\n"
        "| `month` | 최근 30일 |\n"
        "| `all` | 전체 |\n\n"
        "`/summary` 조회 시 자동으로 기록되며, 사용자가 많아질수록 의미있는 데이터가 됩니다."
    ),
    responses={
        401: {"description": "X-API-Key 헤더 누락"},
        403: {"description": "API Key 불일치"},
        429: {"description": "Rate Limit 초과 (분당 30회)"},
    },
)
@limiter.limit("30/minute")
def get_popular(
    request: Request,
    period: str = Query(default="week", pattern="^(today|week|month|all)$", description="집계 기간"),
    limit: int = Query(default=10, ge=1, le=50, description="반환할 최대 기업 수"),
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    query = db.query(
        SearchLog.stock_code,
        SearchLog.company_name,
        func.count(SearchLog.id).label("search_count"),
    )

    days = PERIOD_DAYS[period]
    if days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        query = query.filter(SearchLog.searched_at >= cutoff)

    rows = (
        query
        .group_by(SearchLog.stock_code, SearchLog.company_name)
        .order_by(func.count(SearchLog.id).desc())
        .limit(limit)
        .all()
    )

    return [
        PopularCompany(
            company_name=company_name,
            stock_code=stock_code,
            search_count=search_count,
        )
        for stock_code, company_name, search_count in rows
    ]


# ──────────────────────────────────────────────
# GET /search/popular — 인기 검색 (window 기반, 프론트 연동용)
# realtime=1시간 / daily=24시간 / weekly=7일
# keyword: matched company name 기준 집계
# ──────────────────────────────────────────────

WINDOW_HOURS = {"realtime": 1, "daily": 24, "weekly": 168}


@router.get(
    "/search/popular",
    response_model=PopularSearchResponse,
    summary="인기 검색 기업 (window 기반)",
    description=(
        "검색 횟수 기준 인기 기업을 window 단위로 반환합니다.\n\n"
        "| window | 기간 |\n"
        "|--------|------|\n"
        "| `realtime` | 최근 1시간 |\n"
        "| `daily` | 최근 24시간 |\n"
        "| `weekly` | 최근 7일 |\n\n"
        "**keyword**는 매칭된 기업명(company_name) 기준으로 집계합니다. "
        "같은 기업을 다른 키워드로 검색해도 하나로 합산됩니다."
    ),
    responses={
        401: {"description": "X-API-Key 헤더 누락"},
        403: {"description": "API Key 불일치"},
        429: {"description": "Rate Limit 초과 (분당 30회)"},
    },
)
@limiter.limit("30/minute")
def get_search_popular(
    request: Request,
    window: str = Query(default="realtime", pattern="^(realtime|daily|weekly)$", description="집계 기간"),
    limit: int = Query(default=10, ge=1, le=50, description="반환할 최대 기업 수"),
    db: Session = Depends(get_db),
    current_user=Depends(require_auth),
):
    hours = WINDOW_HOURS[window]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    rows = (
        db.query(
            SearchLog.company_name,
            func.count(SearchLog.id).label("cnt"),
            func.max(SearchLog.searched_at).label("last_searched"),
        )
        .filter(SearchLog.searched_at >= cutoff)
        .group_by(SearchLog.company_name)
        .order_by(func.count(SearchLog.id).desc())
        .limit(limit)
        .all()
    )

    generated_at = datetime.now(timezone.utc)
    overall_updated_at = rows[0].last_searched if rows else None

    items = [
        PopularCompanyItem(
            keyword=row.company_name,
            count=row.cnt,
            rank=idx + 1,
            updated_at=row.last_searched,
        )
        for idx, row in enumerate(rows)
    ]

    return PopularSearchResponse(
        items=items,
        window=window,
        updated_at=overall_updated_at,
        generated_at=generated_at,
    )


# ──────────────────────────────────────────────
# POST /insights/interpret — rule 기반 재무 신호 AI 해석 (Phase 97)
# ──────────────────────────────────────────────

# 프론트 rule 모듈과 합의된 11종 화이트리스트. rule 추가 시 3자(프론트/백엔드/AI) 사전 공유 후 반영.
INSIGHT_RULE_IDS = frozenset({
    "operating-loss-net-profit",
    "revenue-up-operating-profit-down",
    "operating-margin-sharp-decline",
    "net-income-much-higher-than-operating-profit",
    "revenue-and-operating-profit-up",
    "operating-margin-improved",
    "operating-profit-turnaround",
    "operating-profit-and-net-income-up",
    # 현금흐름 3종 (Phase 99 합의, Phase 107에서 실험 rule 1종 정식 편입)
    "net-income-positive-operating-cash-flow-negative",
    "operating-cash-flow-turnaround",
    # Phase 107 — 지배주주 재집계·DART 검증·3자 비준 완료 후 편입 (단년도 caution)
    "operating-cash-flow-much-lower-than-net-income",
})


@router.post(
    "/insights/interpret",
    response_model=InsightInterpretResponse,
    summary="rule 기반 재무 신호 AI 해석",
    description=(
        "프론트 rule 모듈이 탐지한 재무 신호(warning/positive flags)와 근거 수치를 받아 "
        "GPT가 초보자용 자연어 해석을 생성합니다.\n\n"
        "- AI는 전달된 신호와 evidence만 설명하며 수치 재계산·원인 단정·투자 판단을 하지 않습니다.\n"
        "- `rule ID`는 11종 화이트리스트로 검증하며 목록 외 ID는 400입니다.\n"
        "- 같은 flags 입력은 캐시로 재사용합니다 (일일 한도 미소모).\n"
        "- OpenAI 미설정/호출 실패/일일 한도 초과 시 에러 대신 "
        "`interpretation_available=false` 폴백 응답(200)을 반환합니다."
    ),
    responses={
        400: {"description": "목록 외 rule ID 또는 해석할 신호 없음"},
        401: {"description": "X-API-Key 헤더 누락"},
        403: {"description": "API Key 불일치"},
        429: {"description": "Rate Limit 초과 (분당 20회)"},
    },
)
@limiter.limit("20/minute")
def interpret_financial_insights(
    request: Request,
    payload: InsightInterpretRequest,
    current_user=Depends(require_auth),
):
    all_flags = payload.warning_flags + payload.positive_flags
    if not all_flags:
        raise HTTPException(status_code=400, detail="해석할 신호가 없습니다.")
    for flag in all_flags:
        if flag.id not in INSIGHT_RULE_IDS:
            raise HTTPException(status_code=400, detail=f"지원하지 않는 rule ID: {flag.id}")

    # 프롬프트 버전을 키에 포함 — 프롬프트 변경 시 기존 캐시가 새 결과로 오인되지 않도록 (AI 회신 2026-07-15)
    cache_key = ai_analysis_service.interpret_cache_key(
        f"v{INSIGHT_PROMPT_VERSION}:{payload.model_dump_json()}"
    )
    cached = ai_analysis_service.get_interpret_cache(cache_key)
    if cached is not None:
        return InsightInterpretResponse(
            stock_code=payload.stock_code,
            interpretation=InsightInterpretation(**cached),
            interpretation_available=True,
        )

    # 한도 초과는 503이 아닌 폴백 응답 — 프론트 통보 문서(3절)에서 약속한 동작.
    try:
        ai_analysis_service.check_daily_limit()
    except HTTPException as e:
        logger.warning("AI 해석 폴백(일일 한도 초과): %s", e.detail)
        return InsightInterpretResponse(
            stock_code=payload.stock_code,
            interpretation=None,
            interpretation_available=False,
        )

    try:
        # payload 지문이다 — 요청 일련번호가 아니라서 같은 payload 의 재호출은 값이 같다.
        result = interpret_insights(payload, request_id=cache_key[:12])
    except Exception as e:
        logger.warning("AI 해석 폴백(생성 실패): %s", e)
        return InsightInterpretResponse(
            stock_code=payload.stock_code,
            interpretation=None,
            interpretation_available=False,
        )

    headline_mismatch = result.pop("_headline_year_mismatch", False)
    if not headline_mismatch:
        ai_analysis_service.set_interpret_cache(cache_key, result)
    ai_analysis_service.increment_daily_counter()
    return InsightInterpretResponse(
        stock_code=payload.stock_code,
        interpretation=InsightInterpretation(**result),
        interpretation_available=True,
    )


# ──────────────────────────────────────────────
# GET /disclosures — 신호 기반 DART 공시 검색 (Phase 98)
# ──────────────────────────────────────────────

# report_nm 포함 키워드(공백 제거 후 매칭) → category
_PERIODIC_KEYWORDS = ("사업보고서", "반기보고서", "분기보고서")
_PERFORMANCE_KEYWORDS = ("영업(잠정)실적", "손익구조")   # "매출액또는손익구조30%..." 포함
_MAJOR_KEYWORDS = ("주요사항보고서",)

_WARNING_RULE_IDS = frozenset({
    "operating-loss-net-profit",
    "revenue-up-operating-profit-down",
    "operating-margin-sharp-decline",
    "net-income-much-higher-than-operating-profit",
    # 현금흐름 warning 2종 (Phase 99 합의, Phase 107에서 실험 rule 1종 정식 편입 — 단년도 caution)
    "net-income-positive-operating-cash-flow-negative",
    "operating-cash-flow-much-lower-than-net-income",
})

_DISCLOSURE_MAX_ITEMS = 15


def _classify_disclosure(report_nm: str) -> str:
    name = re.sub(r"\s+", "", report_nm)
    if any(k in name for k in _PERIODIC_KEYWORDS):
        return "periodic"
    if any(k in name for k in _PERFORMANCE_KEYWORDS):
        return "performance"
    if any(k in name for k in _MAJOR_KEYWORDS):
        return "major"
    return "other"


@router.get(
    "/disclosures",
    response_model=DisclosureListResponse,
    summary="신호 기반 DART 공시 목록",
    description=(
        "rule 신호가 탐지된 기업의 근거가 될 만한 DART 공시(최근 1년)를 원문 링크와 함께 반환합니다.\n\n"
        "- 손익 신호와 관련된 공시만 필터링: 정기보고서(periodic) · 실적/손익구조(performance) · 주요사항보고서(major)\n"
        "- `rule_id`가 주의 신호(6종)면 major 포함, 긍정 신호(5종)면 major 제외, 미지정이면 전부 포함\n"
        "- `rule_id`는 11종 화이트리스트(Phase 97 10종 + Phase 107 1종)로 검증하며 목록 외 ID는 400입니다.\n"
        "- 같은 종목은 당일 캐시로 재사용해 DART 쿼터를 소모하지 않습니다."
    ),
    responses={
        400: {"description": "목록 외 rule ID"},
        401: {"description": "X-API-Key 헤더 누락"},
        403: {"description": "API Key 불일치"},
        404: {"description": "종목코드에 해당하는 기업 없음"},
        429: {"description": "Rate Limit 초과 (분당 20회)"},
        503: {"description": "기업 리스트 로딩 중 또는 DART 공시 조회 실패"},
    },
)
@limiter.limit("20/minute")
def get_signal_disclosures(
    request: Request,
    stock_code: str = Query(..., pattern=r"^\d{6}$", description="6자리 종목코드"),
    rule_id: Optional[str] = Query(None, description="rule ID (11종 화이트리스트, 선택)"),
    current_user=Depends(require_auth),
):
    if rule_id is not None and rule_id not in INSIGHT_RULE_IDS:
        raise HTTPException(status_code=400, detail=f"지원하지 않는 rule ID: {rule_id}")

    try:
        corp = DartService.get_corp_by_stock_code(stock_code)
    except ServiceNotReadyError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except CompanyNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))

    try:
        rows = DartService.fetch_recent_disclosures(corp.corp_code)
    except Exception as e:
        logger.warning("공시 목록 조회 실패(%s): %s", stock_code, e)
        raise HTTPException(status_code=503, detail="DART 공시 조회에 실패했습니다. 잠시 후 다시 시도해주세요.")

    if rule_id is None or rule_id in _WARNING_RULE_IDS:
        allowed = {"periodic", "performance", "major"}
    else:
        allowed = {"periodic", "performance"}

    items = []
    for row in rows:
        category = _classify_disclosure(str(row.get("report_nm", "")))
        if category not in allowed:
            continue
        rcept_dt = str(row.get("rcept_dt", ""))
        items.append(DisclosureItem(
            title=str(row.get("report_nm", "")).strip(),
            date=f"{rcept_dt[:4]}-{rcept_dt[4:6]}-{rcept_dt[6:8]}" if len(rcept_dt) == 8 else rcept_dt,
            submitter=str(row.get("flr_nm", "")),
            viewer_url=f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={row.get('rcept_no', '')}",
            category=category,
        ))
        if len(items) >= _DISCLOSURE_MAX_ITEMS:
            break

    return DisclosureListResponse(
        stock_code=stock_code,
        company=corp.corp_name,
        rule_id=rule_id,
        disclosures=items,
    )
