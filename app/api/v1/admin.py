"""관리자 전용 API - 통계 조회."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import CollectionTask, FinancialStatement, Inquiry, SearchLog, User
from app.dependencies.auth import get_admin_user
from app.dependencies.rate_limit import limiter
from app.services.dart_service import DartService

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/admin", tags=["Admin"])


async def _admin_force_collect(stock_code: str) -> None:
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, DartService.fetch_and_process_data, stock_code)
    except Exception as e:
        logger.error("관리자 강제 재수집 실패 [%s]: %s", stock_code, e)


class AdminStatsResponse(BaseModel):
    total_users: int
    active_users: int
    verified_users: int
    collected_companies: int
    failed_collections: int
    failed_stock_codes: List[str]
    unanswered_inquiries: int
    today_searches: int
    searches_30d: int


class AdminUserItem(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    email: str
    nickname: Optional[str]
    membership_tier: str
    is_active: bool
    is_admin: bool
    is_verified: bool
    created_at: datetime


class AdminUserListResponse(BaseModel):
    items: List[AdminUserItem]
    total: int
    page: int
    size: int


@router.get(
    "/stats",
    response_model=AdminStatsResponse,
    summary="관리자 통계",
    description="서비스 현황 통계를 반환합니다. 관리자 계정(is_admin=True) 전용.",
    responses={
        401: {"description": "로그인 필요"},
        403: {"description": "관리자 권한 없음"},
    },
)
@limiter.limit("30/minute")
def get_admin_stats(
    request: Request,
    db: Session = Depends(get_db),
    _: None = Depends(get_admin_user),
):
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    total_users = db.query(func.count(User.id)).scalar()
    active_users = db.query(func.count(User.id)).filter(User.is_active.is_(True)).scalar()
    verified_users = db.query(func.count(User.id)).filter(User.is_verified.is_(True)).scalar()
    collected_companies = db.query(func.count(func.distinct(FinancialStatement.company_id))).scalar()
    failed_tasks = db.query(CollectionTask).filter(CollectionTask.status == "failed").all()
    failed_collections = len(failed_tasks)
    failed_stock_codes = [task.stock_code for task in failed_tasks]
    unanswered_inquiries = db.query(func.count(Inquiry.id)).filter(Inquiry.status == "pending").scalar()
    today_searches = db.query(func.count(SearchLog.id)).filter(SearchLog.searched_at >= today_start).scalar()
    searches_30d = db.query(func.count(SearchLog.id)).filter(
        SearchLog.searched_at >= now - timedelta(days=30)
    ).scalar()

    return AdminStatsResponse(
        total_users=total_users or 0,
        active_users=active_users or 0,
        verified_users=verified_users or 0,
        collected_companies=collected_companies or 0,
        failed_collections=failed_collections,
        failed_stock_codes=failed_stock_codes,
        unanswered_inquiries=unanswered_inquiries or 0,
        today_searches=today_searches or 0,
        searches_30d=searches_30d or 0,
    )


@router.get(
    "/users",
    response_model=AdminUserListResponse,
    summary="회원 목록 (관리자)",
)
@limiter.limit("30/minute")
def list_users(
    request: Request,
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
    membership_tier: Optional[str] = Query(default=None),
    is_admin: Optional[bool] = Query(default=None),
    db: Session = Depends(get_db),
    _: None = Depends(get_admin_user),
):
    query = db.query(User)
    if membership_tier is not None:
        query = query.filter(User.membership_tier == membership_tier)
    if is_admin is not None:
        query = query.filter(User.is_admin.is_(is_admin))

    total = query.count()
    users = (
        query.order_by(User.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return AdminUserListResponse(items=users, total=total, page=page, size=size)


@router.post(
    "/collect/refresh-stale",
    status_code=202,
    summary="Refresh stale collection tasks",
    description=(
        "Refresh completed collection tasks older than stale_days in the background. "
        "Use /collect/status to check completion."
    ),
    responses={
        503: {"description": "DART service initializing"},
    },
)
@limiter.limit("3/hour")
def admin_refresh_stale(
    request: Request,
    background_tasks: BackgroundTasks,
    stale_days: int = Query(default=7, ge=1, le=365, description="Days before data is stale"),
    db: Session = Depends(get_db),
    _: None = Depends(get_admin_user),
):
    if DartService._init_failed or not DartService._stock_corps:
        raise HTTPException(status_code=503, detail="DART 서비스 초기화 중입니다.")

    cutoff = datetime.now(timezone.utc) - timedelta(days=stale_days)
    completed_tasks = db.query(CollectionTask).filter(CollectionTask.status == "completed").all()

    stale_tasks = []
    for task in completed_tasks:
        if task.updated_at is None:
            stale_tasks.append(task)
            continue
        updated_at = task.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if updated_at < cutoff:
            stale_tasks.append(task)

    now = datetime.now(timezone.utc)
    triggered = []
    for task in stale_tasks:
        task.status = "processing"
        task.message = f"{stale_days}일 초과 자동 갱신"
        task.started_at = now
        task.updated_at = now
        background_tasks.add_task(_admin_force_collect, task.stock_code)
        triggered.append(task.stock_code)

    db.commit()
    return {"triggered": len(triggered), "stock_codes": triggered}


@router.post(
    "/dart/reinitialize",
    summary="DART 기업 리스트 재초기화 (관리자)",
    description=(
        "DART 기업 리스트 초기화가 실패해 서비스가 degraded 상태일 때, "
        "서버 재시작 없이 백그라운드로 다시 초기화합니다.\n\n"
        "| status | 의미 |\n"
        "|--------|------|\n"
        "| `reinitializing` | 재초기화 시작됨 — `/health`의 `corp_list`로 진행 확인 |\n"
        "| `already_ready` | 이미 로딩 완료 — 재초기화 불필요 |\n"
        "| `loading` | 최초 로딩 진행 중 — 재초기화 불필요 |\n"
        "| `already_running` | 재초기화가 이미 진행 중 |"
    ),
    responses={
        401: {"description": "로그인 필요"},
        403: {"description": "관리자 권한 없음"},
    },
)
@limiter.limit("3/minute")
def reinitialize_dart(
    request: Request,
    _: None = Depends(get_admin_user),
):
    status = DartService.trigger_reinitialize()
    return {"status": status}


@router.post(
    "/collect/{stock_code}",
    status_code=202,
    summary="기업 데이터 강제 재수집 (관리자)",
    description="수집 실패 종목을 즉시 재수집합니다. 수집은 백그라운드에서 진행됩니다.",
)
@limiter.limit("10/minute")
def admin_force_collect(
    stock_code: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _: None = Depends(get_admin_user),
):
    if DartService._init_failed or not DartService._stock_corps:
        raise HTTPException(status_code=503, detail="DART 서비스 초기화 중입니다.")

    task = db.query(CollectionTask).filter(CollectionTask.stock_code == stock_code).first()
    now = datetime.now(timezone.utc)
    message = "관리자 강제 재수집"
    if task:
        task.status = "processing"
        task.message = message
        task.started_at = now
        task.updated_at = now
    else:
        task = CollectionTask(
            stock_code=stock_code,
            status="processing",
            message=message,
            started_at=now,
            updated_at=now,
        )
        db.add(task)
    db.commit()

    background_tasks.add_task(_admin_force_collect, stock_code)
    return {"status": "collecting", "stock_code": stock_code}
