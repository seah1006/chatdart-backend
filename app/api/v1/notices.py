import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Body
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.db.models import Notice, Inquiry, User
from datetime import datetime, timezone
from app.dependencies.auth import get_admin_user
from app.dependencies.rate_limit import limiter
from app.schemas.notice_schema import (
    NoticeCreateRequest, NoticeUpdateRequest,
    NoticeResponse, NoticeListResponse,
)
from app.schemas.user_schema import InquiryResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/notices", tags=["Notices"])


@router.get(
    "",
    response_model=NoticeListResponse,
    summary="공지사항 목록",
    description="고정 공지를 먼저, 나머지는 최신순으로 반환합니다. 인증 불필요.",
)
@limiter.limit("30/minute")
def list_notices(
    request: Request,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
):
    query = db.query(Notice).order_by(Notice.is_pinned.desc(), Notice.created_at.desc())
    total = query.count()
    items = query.offset((page - 1) * size).limit(size).all()
    return NoticeListResponse(items=items, total=total, page=page, size=size)


@router.get(
    "/{notice_id}",
    response_model=NoticeResponse,
    summary="공지사항 상세",
    description="인증 불필요.",
)
@limiter.limit("30/minute")
def get_notice(request: Request, notice_id: int, db: Session = Depends(get_db)):
    notice = db.query(Notice).filter(Notice.id == notice_id).first()
    if not notice:
        raise HTTPException(status_code=404, detail="공지사항을 찾을 수 없습니다.")
    return notice


@router.post(
    "",
    response_model=NoticeResponse,
    status_code=201,
    summary="공지사항 작성 (관리자)",
)
@limiter.limit("10/minute")
def create_notice(
    request: Request,
    body: NoticeCreateRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    notice = Notice(title=body.title, content=body.content, is_pinned=body.is_pinned, author_id=admin.id)
    db.add(notice)
    db.commit()
    db.refresh(notice)
    logger.info("공지사항 작성: id=%s, admin=%s", notice.id, admin.id)
    return notice


@router.patch(
    "/{notice_id}",
    response_model=NoticeResponse,
    summary="공지사항 수정 (관리자)",
)
@limiter.limit("10/minute")
def update_notice(
    request: Request,
    notice_id: int,
    body: NoticeUpdateRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    notice = db.query(Notice).filter(Notice.id == notice_id).first()
    if not notice:
        raise HTTPException(status_code=404, detail="공지사항을 찾을 수 없습니다.")
    if body.title is not None:
        notice.title = body.title
    if body.content is not None:
        notice.content = body.content
    if body.is_pinned is not None:
        notice.is_pinned = body.is_pinned
    db.commit()
    db.refresh(notice)
    return notice


@router.delete("/{notice_id}", status_code=204, summary="공지사항 삭제 (관리자)")
@limiter.limit("10/minute")
def delete_notice(
    request: Request,
    notice_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    notice = db.query(Notice).filter(Notice.id == notice_id).first()
    if not notice:
        raise HTTPException(status_code=404, detail="공지사항을 찾을 수 없습니다.")
    db.delete(notice)
    db.commit()


# ── 관리자 문의 관리 ──────────────────────────────────────────────────────────

@router.get(
    "/admin/inquiries",
    response_model=List[InquiryResponse],
    summary="전체 문의 목록 (관리자)",
    description="모든 유저의 문의를 최신순으로 반환합니다.",
)
@limiter.limit("30/minute")
def list_all_inquiries(
    request: Request,
    status: str = Query(default="all", pattern="^(all|pending|answered)$"),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    query = db.query(Inquiry).order_by(Inquiry.created_at.desc())
    if status != "all":
        query = query.filter(Inquiry.status == status)
    return query.offset((page - 1) * size).limit(size).all()


@router.patch(
    "/admin/inquiries/{inquiry_id}/answer",
    response_model=InquiryResponse,
    summary="문의 답변 작성 (관리자)",
)
@limiter.limit("20/minute")
def answer_inquiry(
    request: Request,
    inquiry_id: int,
    body: dict = Body(...),
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
):
    answer_text = body.get("answer", "").strip()
    if not answer_text:
        raise HTTPException(status_code=400, detail="답변 내용을 입력해 주세요.")

    inquiry = db.query(Inquiry).filter(Inquiry.id == inquiry_id).first()
    if not inquiry:
        raise HTTPException(status_code=404, detail="문의를 찾을 수 없습니다.")

    inquiry.answer = answer_text
    inquiry.status = "answered"
    inquiry.answered_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(inquiry)
    logger.info("문의 답변: inquiry_id=%s, admin=%s", inquiry_id, admin.id)
    return inquiry
