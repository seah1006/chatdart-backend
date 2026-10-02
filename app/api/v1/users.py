import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.security import verify_password, hash_password
from app.db.database import get_db
from app.db.models import (
    User, Watchlist, SearchLog, Inquiry, PaymentRecord,
    CollectionTask, FinancialStatement,
)
from app.dependencies.auth import get_current_user
from app.dependencies.rate_limit import limiter
from app.schemas.auth_schema import UserResponse
from app.schemas.user_schema import (
    UpdateUserRequest, UsageResponse,
    WatchlistItem, WatchlistAddRequest, WatchlistAddResponse,
    WatchlistUpdateRequest, WatchlistSummaryItem,
    DashboardResponse, DashboardPopularItem, DashboardRecentItem,
    HistoryItem,
    InquiryCreateRequest, InquiryResponse,
    PaymentHistoryItem,
)
from app.services.analysis import evaluate_growth, evaluate_stability, evaluate_profitability
from app.services.dart_service import DartService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/users", tags=["Users"])


async def _auto_collect(stock_code: str) -> None:
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, DartService.fetch_and_process_data, stock_code)
    except Exception as e:
        logger.warning("즐겨찾기 자동 수집 실패 [%s]: %s", stock_code, e)


# ── 회원 정보 ─────────────────────────────────────────────────────────────────

@router.get("/me", response_model=UserResponse, summary="내 정보 조회")
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.patch(
    "/me",
    response_model=UserResponse,
    summary="회원 정보 수정",
    description="닉네임 또는 비밀번호를 변경합니다. 비밀번호 변경 시 다른 기기 세션이 무효화됩니다.",
)
@limiter.limit("10/minute")
def update_me(
    request: Request,
    body: UpdateUserRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.nickname is not None:
        current_user.nickname = body.nickname

    if body.new_password is not None:
        if not body.current_password:
            raise HTTPException(status_code=400, detail="현재 비밀번호를 입력해 주세요.")
        if not verify_password(body.current_password, current_user.hashed_password):
            raise HTTPException(status_code=400, detail="현재 비밀번호가 올바르지 않습니다.")
        current_user.hashed_password = hash_password(body.new_password)
        current_user.refresh_token_hash = None
        current_user.tokens_valid_after = datetime.now(timezone.utc)

    db.commit()
    db.refresh(current_user)
    return current_user


@router.delete("/me", status_code=204, summary="회원 탈퇴")
def delete_me(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    current_user.is_active = False
    current_user.refresh_token_hash = None
    db.commit()
    logger.info("회원 탈퇴: user_id=%s", current_user.id)


WATCHLIST_LIMIT = {"free": 5, "pro": 50}


_POPULAR_WEEK_CACHE_TTL_SECONDS = 300
_popular_week_cache: dict = {"value": None, "expires_at": None}


def _build_watchlist_summary_items(
    items: List[Watchlist],
    db: Session,
) -> List[WatchlistSummaryItem]:
    result: List[WatchlistSummaryItem] = []
    for item in items:
        records = (
            db.query(FinancialStatement)
            .filter(FinancialStatement.company_id == item.stock_code)
            .order_by(FinancialStatement.fiscal_year.desc())
            .limit(2)
            .all()
        )
        if not records:
            task = (
                db.query(CollectionTask)
                .filter(CollectionTask.stock_code == item.stock_code)
                .first()
            )
            status = "collecting" if task and task.status in ("pending", "processing") else "none"
            result.append(WatchlistSummaryItem(
                stock_code=item.stock_code,
                company_name=item.company_name,
                status=status,
            ))
            continue

        latest = records[0]
        previous = records[1] if len(records) > 1 else None
        result.append(WatchlistSummaryItem(
            stock_code=item.stock_code,
            company_name=item.company_name,
            fiscal_year=latest.fiscal_year,
            revenue=latest.revenue,
            operating_profit=latest.operating_profit,
            growth_status=evaluate_growth(latest, previous),
            profitability_status=evaluate_profitability(latest, previous),
            stability_status=evaluate_stability(latest),
            status="ready",
        ))
    return result


def _get_popular_week_cached(db: Session) -> List[DashboardPopularItem]:
    now = datetime.now(timezone.utc)
    expires_at = _popular_week_cache.get("expires_at")
    cached_value = _popular_week_cache.get("value")
    if cached_value is not None and expires_at and now < expires_at:
        return cached_value

    cutoff_week = now - timedelta(days=7)
    rows = (
        db.query(
            SearchLog.stock_code,
            SearchLog.company_name,
            func.count(SearchLog.id).label("cnt"),
        )
        .filter(SearchLog.searched_at >= cutoff_week)
        .group_by(SearchLog.stock_code, SearchLog.company_name)
        .order_by(func.count(SearchLog.id).desc())
        .limit(5)
        .all()
    )
    value = [
        DashboardPopularItem(stock_code=code, company_name=name, search_count=count)
        for code, name, count in rows
    ]
    _popular_week_cache["value"] = value
    _popular_week_cache["expires_at"] = now + timedelta(seconds=_POPULAR_WEEK_CACHE_TTL_SECONDS)
    return value


@router.get(
    "/me/usage",
    response_model=UsageResponse,
    summary="사용량 조회",
    description="현재 멤버십 등급, 즐겨찾기 수, 즐겨찾기 한도, 가입일을 반환합니다.",
)
def get_usage(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    watchlist_count = db.query(Watchlist).filter(Watchlist.user_id == current_user.id).count()
    tier = current_user.membership_tier
    return UsageResponse(
        membership_tier=tier,
        watchlist_count=watchlist_count,
        watchlist_limit=WATCHLIST_LIMIT.get(tier, 5),
        account_created_at=current_user.created_at,
    )


# ── 즐겨찾기 ──────────────────────────────────────────────────────────────────

@router.get("/me/watchlist", response_model=List[WatchlistItem], summary="즐겨찾기 목록")
def get_watchlist(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return (
        db.query(Watchlist)
        .filter(Watchlist.user_id == current_user.id)
        .order_by(Watchlist.added_at.desc())
        .all()
    )


@router.post(
    "/me/watchlist",
    response_model=WatchlistAddResponse,
    status_code=201,
    summary="즐겨찾기 추가",
)
@limiter.limit("30/minute")
def add_watchlist(
    request: Request,
    body: WatchlistAddRequest,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    existing = db.query(Watchlist).filter(
        Watchlist.user_id == current_user.id,
        Watchlist.stock_code == body.stock_code,
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="이미 즐겨찾기에 추가된 종목입니다.")

    tier = current_user.membership_tier
    limit = WATCHLIST_LIMIT.get(tier, 5)
    count = db.query(Watchlist).filter(Watchlist.user_id == current_user.id).count()
    if count >= limit:
        raise HTTPException(
            status_code=403,
            detail=f"{tier.upper()} 플랜은 즐겨찾기를 최대 {limit}개까지 추가할 수 있습니다. Pro로 업그레이드하세요.",
        )

    item = Watchlist(
        user_id=current_user.id,
        stock_code=body.stock_code,
        company_name=body.company_name,
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    dart_ready = not DartService._init_failed and bool(DartService._stock_corps)
    if dart_ready:
        existing_task = db.query(CollectionTask).filter(
            CollectionTask.stock_code == body.stock_code,
            CollectionTask.status.in_(["completed", "processing"]),
        ).first()
        if not existing_task:
            task = db.query(CollectionTask).filter(CollectionTask.stock_code == body.stock_code).first()
            now = datetime.now(timezone.utc)
            if task:
                task.status = "processing"
                task.message = "즐겨찾기 자동 수집"
                task.started_at = now
                task.updated_at = now
            else:
                db.add(CollectionTask(
                    stock_code=body.stock_code,
                    status="processing",
                    message="즐겨찾기 자동 수집",
                    started_at=now,
                    updated_at=now,
                ))
            db.commit()
            background_tasks.add_task(_auto_collect, body.stock_code)

    new_count = db.query(Watchlist).filter(Watchlist.user_id == current_user.id).count()
    return WatchlistAddResponse(
        item=WatchlistItem.model_validate(item),
        watchlist_count=new_count,
        watchlist_limit=limit,
    )


@router.get(
    "/me/watchlist/summaries",
    response_model=List[WatchlistSummaryItem],
    summary="즐겨찾기 종목 일괄 요약",
)
@limiter.limit("20/minute")
def get_watchlist_summaries(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    items = (
        db.query(Watchlist)
        .filter(Watchlist.user_id == current_user.id)
        .order_by(Watchlist.added_at.desc())
        .all()
    )
    return _build_watchlist_summary_items(items, db)


@router.get(
    "/me/dashboard",
    response_model=DashboardResponse,
    summary="대시보드 통합 정보",
)
@limiter.limit("30/minute")
def get_dashboard(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tier = current_user.membership_tier
    limit = WATCHLIST_LIMIT.get(tier, 5)
    watchlist_count = db.query(Watchlist).filter(Watchlist.user_id == current_user.id).count()

    watch_items = (
        db.query(Watchlist)
        .filter(Watchlist.user_id == current_user.id)
        .order_by(Watchlist.added_at.desc())
        .limit(5)
        .all()
    )
    preview = _build_watchlist_summary_items(watch_items, db)
    popular_week = _get_popular_week_cached(db)

    recent_rows = (
        db.query(SearchLog.stock_code, SearchLog.company_name, SearchLog.searched_at)
        .filter(SearchLog.user_id == current_user.id)
        .order_by(SearchLog.searched_at.desc())
        .limit(5)
        .all()
    )
    recent_searches = [
        DashboardRecentItem(stock_code=code, company_name=name, searched_at=searched_at)
        for code, name, searched_at in recent_rows
    ]

    return DashboardResponse(
        membership_tier=tier,
        watchlist_count=watchlist_count,
        watchlist_limit=limit,
        watchlist_preview=preview,
        popular_week=popular_week,
        recent_searches=recent_searches,
    )


@router.delete("/me/watchlist/{stock_code}", status_code=204, summary="즐겨찾기 삭제")
def remove_watchlist(
    stock_code: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.query(Watchlist).filter(
        Watchlist.user_id == current_user.id,
        Watchlist.stock_code == stock_code,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="즐겨찾기에 없는 종목입니다.")
    db.delete(item)
    db.commit()


# ── 조회 히스토리 ─────────────────────────────────────────────────────────────

@router.patch(
    "/me/watchlist/{stock_code}",
    response_model=WatchlistItem,
    summary="Update watchlist memo",
)
@limiter.limit("30/minute")
def update_watchlist_memo(
    request: Request,
    stock_code: str,
    body: WatchlistUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    item = db.query(Watchlist).filter(
        Watchlist.user_id == current_user.id,
        Watchlist.stock_code == stock_code,
    ).first()
    if not item:
        raise HTTPException(status_code=404, detail="즐겨찾기에 없는 종목입니다.")

    item.memo = body.memo
    db.commit()
    db.refresh(item)
    return item


@router.get(
    "/me/history",
    response_model=List[HistoryItem],
    summary="조회 히스토리",
    description="최근 조회한 기업 목록. finance 엔드포인트에 JWT 통합 후 채워집니다.",
)
def get_history(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=100),
):
    return (
        db.query(SearchLog)
        .filter(SearchLog.user_id == current_user.id)
        .order_by(SearchLog.searched_at.desc())
        .limit(limit)
        .all()
    )


@router.delete("/me/history", status_code=204, summary="조회 히스토리 전체 삭제")
def clear_history(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    db.query(SearchLog).filter(SearchLog.user_id == current_user.id).delete()
    db.commit()


# ── 결제 내역 ─────────────────────────────────────────────────────────────────

@router.get(
    "/me/payments",
    response_model=List[PaymentHistoryItem],
    summary="결제 내역 조회",
    description="내 결제 내역을 최신순으로 반환합니다.",
)
def get_payments(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
):
    return (
        db.query(PaymentRecord)
        .filter(PaymentRecord.user_id == current_user.id)
        .order_by(PaymentRecord.created_at.desc())
        .limit(limit)
        .all()
    )


# ── 문의하기 ──────────────────────────────────────────────────────────────────

@router.post(
    "/me/inquiries",
    response_model=InquiryResponse,
    status_code=201,
    summary="문의 접수",
)
@limiter.limit("5/minute")
def create_inquiry(
    request: Request,
    body: InquiryCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    inquiry = Inquiry(user_id=current_user.id, title=body.title, content=body.content)
    db.add(inquiry)
    db.commit()
    db.refresh(inquiry)
    return inquiry


@router.get("/me/inquiries", response_model=List[InquiryResponse], summary="내 문의 목록")
def get_inquiries(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return (
        db.query(Inquiry)
        .filter(Inquiry.user_id == current_user.id)
        .order_by(Inquiry.created_at.desc())
        .all()
    )


@router.get("/me/inquiries/{inquiry_id}", response_model=InquiryResponse, summary="문의 상세")
def get_inquiry(
    inquiry_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    inquiry = db.query(Inquiry).filter(
        Inquiry.id == inquiry_id,
        Inquiry.user_id == current_user.id,
    ).first()
    if not inquiry:
        raise HTTPException(status_code=404, detail="문의를 찾을 수 없습니다.")
    return inquiry
