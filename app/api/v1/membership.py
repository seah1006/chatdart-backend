import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session
from sqlalchemy.sql import func

from app.core.config import settings
from app.db.database import get_db
from app.db.models import User, UserMembership, PaymentRecord, Watchlist
from app.dependencies.auth import get_current_user
from app.dependencies.rate_limit import limiter
from app.api.v1.users import WATCHLIST_LIMIT
from app.services.pg_service import verify_payment, verify_webhook_signature
from app.schemas.membership_schema import (
    PlanInfo, MembershipResponse,
    SubscribeRequest, SubscribeResponse,
    WebhookRequest, CancelResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/membership", tags=["Membership"])

MEMBERSHIP_PLANS: dict[str, PlanInfo] = {
    "pro": PlanInfo(plan="pro", name="Pro 플랜", price=9900, duration_days=30),
}


def _development_webhook_secret_matches(received: str | None) -> bool:
    """Development-only fallback for manual webhook tests."""
    return bool(received and settings.WEBHOOK_SECRET and secrets.compare_digest(received, settings.WEBHOOK_SECRET))


def _raise_invalid_webhook_signature() -> None:
    raise HTTPException(status_code=401, detail="Webhook signature is invalid or missing.")


@router.get("/plans", response_model=List[PlanInfo], summary="멤버십 플랜 목록")
def get_plans():
    return list(MEMBERSHIP_PLANS.values())


@router.get(
    "/me",
    response_model=MembershipResponse,
    summary="내 멤버십 조회",
    description="활성 또는 취소 후 만료 전 멤버십을 반환합니다. 만료된 경우 자동으로 free로 다운그레이드합니다.",
)
def get_my_membership(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    watchlist_count = db.query(Watchlist).filter(Watchlist.user_id == current_user.id).count()
    membership = (
        db.query(UserMembership)
        .filter(
            UserMembership.user_id == current_user.id,
            UserMembership.status.in_(["active", "cancelled"]),
        )
        .order_by(UserMembership.started_at.desc())
        .first()
    )
    if membership and membership.status == "cancelled" and membership.expires_at:
        expires_at = membership.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < now:
            membership = None

    if not membership:
        return MembershipResponse(
            plan="free",
            started_at=current_user.created_at,
            expires_at=None,
            status="active",
            watchlist_count=watchlist_count,
            watchlist_limit=WATCHLIST_LIMIT.get(current_user.membership_tier, 5),
        )

    days_until_expiry: Optional[int] = None
    if membership.expires_at:
        # SQLite는 timezone-naive datetime을 반환할 수 있으므로 UTC로 보정 후 비교
        expires_at = membership.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at < now:
            membership.status = "expired"
            current_user.membership_tier = "free"
            db.commit()
            return MembershipResponse(
                plan="free",
                started_at=current_user.created_at,
                expires_at=None,
                status="active",
                watchlist_count=watchlist_count,
                watchlist_limit=WATCHLIST_LIMIT.get(current_user.membership_tier, 5),
            )
        days_until_expiry = max(0, (expires_at - now).days)
        if days_until_expiry <= 7 and not membership.warning_sent:
            membership.warning_sent = True
            db.commit()

    return MembershipResponse(
        plan=membership.plan,
        started_at=membership.started_at,
        expires_at=membership.expires_at,
        status=membership.status,
        days_until_expiry=days_until_expiry,
        watchlist_count=watchlist_count,
        watchlist_limit=WATCHLIST_LIMIT.get(current_user.membership_tier, 5),
    )


@router.post(
    "/subscribe",
    response_model=SubscribeResponse,
    summary="구독 시작",
    description="결제 전 단계. PG사 결제 시 사용할 payment_id를 발급합니다. 결제 완료 후 /webhook으로 활성화하세요.",
)
@limiter.limit("5/minute")
def subscribe(
    request: Request,
    body: SubscribeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    plan = MEMBERSHIP_PLANS.get(body.plan)
    if not plan:
        raise HTTPException(status_code=400, detail=f"존재하지 않는 플랜입니다: {body.plan}")

    payment_id = f"chatdart_{secrets.token_hex(8)}"
    record = PaymentRecord(
        user_id=current_user.id,
        plan=body.plan,
        amount=plan.price,
        payment_id=payment_id,
        status="pending",
    )
    db.add(record)
    db.commit()
    logger.info("결제 시작: user_id=%s, plan=%s, payment_id=%s", current_user.id, body.plan, payment_id)
    return SubscribeResponse(payment_id=payment_id, amount=plan.price, plan=body.plan)


@router.post(
    "/webhook",
    summary="결제 웹훅 수신",
    description="PG사(포트원·토스페이먼츠)에서 결제 결과를 전송하는 엔드포인트. PG API로 금액·상태를 검증 후 멤버십을 활성화합니다.",
)
async def payment_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    raw_body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}

    if not verify_webhook_signature(raw_body, headers):
        if not settings.is_development or not _development_webhook_secret_matches(headers.get("x-webhook-secret")):
            _raise_invalid_webhook_signature()

    try:
        body = WebhookRequest.model_validate_json(raw_body)
    except ValidationError:
        raise HTTPException(status_code=400, detail="Invalid webhook request body.")

    record = db.query(PaymentRecord).filter(PaymentRecord.payment_id == body.payment_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="결제 정보를 찾을 수 없습니다.")
    if record.status == "paid":
        return {"message": "이미 처리된 결제입니다."}

    if body.status != "paid":
        record.status = "failed"
        db.commit()
        raise HTTPException(status_code=400, detail="결제 실패 상태입니다.")

    # PG사 API 호출로 금액·상태 검증
    is_valid = await verify_payment(body.pg_transaction_id, record.amount)
    if not is_valid:
        record.status = "failed"
        db.commit()
        raise HTTPException(status_code=400, detail="PG사 결제 검증 실패. 금액 또는 상태가 일치하지 않습니다.")

    plan_info = MEMBERSHIP_PLANS.get(record.plan)
    if not plan_info:
        raise HTTPException(status_code=400, detail="유효하지 않은 플랜입니다.")

    updated = (
        db.query(PaymentRecord)
        .filter(PaymentRecord.payment_id == body.payment_id, PaymentRecord.status == "pending")
        .update(
            {
                "status": "paid",
                "pg_transaction_id": body.pg_transaction_id,
                "updated_at": func.now(),
            },
            synchronize_session=False,
        )
    )
    if not updated:
        db.rollback()
        return {"message": "이미 처리된 결제입니다."}

    now = datetime.now(timezone.utc)
    membership = UserMembership(
        user_id=record.user_id,
        plan=record.plan,
        started_at=now,
        expires_at=now + timedelta(days=plan_info.duration_days),
        status="active",
    )
    db.add(membership)

    user = db.query(User).filter(User.id == record.user_id).first()
    if user:
        user.membership_tier = record.plan

    db.commit()
    logger.info("멤버십 활성화: user_id=%s, plan=%s, expires=%s", record.user_id, record.plan, membership.expires_at)
    return {"message": "멤버십이 활성화되었습니다."}


@router.post(
    "/cancel",
    response_model=CancelResponse,
    summary="멤버십 해지",
    description="해지 후 만료일까지는 Pro 혜택이 유지됩니다.",
)
@limiter.limit("3/minute")
def cancel_membership(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    membership = (
        db.query(UserMembership)
        .filter(UserMembership.user_id == current_user.id, UserMembership.status == "active")
        .order_by(UserMembership.started_at.desc())
        .first()
    )
    if not membership or membership.plan == "free":
        raise HTTPException(status_code=400, detail="활성화된 유료 멤버십이 없습니다.")

    membership.status = "cancelled"
    db.commit()
    logger.info("멤버십 해지: user_id=%s, plan=%s", current_user.id, membership.plan)

    expires_str = membership.expires_at.strftime("%Y-%m-%d") if membership.expires_at else ""
    message = (
        f"멤버십이 해지되었습니다. {expires_str}까지 Pro 혜택이 유지됩니다."
        if expires_str else "멤버십이 해지되었습니다."
    )
    return CancelResponse(status="cancelled", message=message)
