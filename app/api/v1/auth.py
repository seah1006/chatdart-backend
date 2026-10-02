import asyncio
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import (
    hash_password, verify_password,
    create_access_token, create_refresh_token,
    hash_token, decode_token,
)
from app.db.database import get_db
from app.db.models import User, PasswordResetToken, EmailVerificationToken
from app.dependencies.auth import get_current_user
from app.dependencies.rate_limit import limiter
from app.schemas.auth_schema import (
    RegisterRequest, LoginRequest,
    TokenResponse, RefreshRequest, AccessTokenResponse, UserResponse, RegisterResponse,
    ForgotPasswordRequest, ResetPasswordRequest,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/auth", tags=["Auth"])


class VerifyEmailRequest(BaseModel):
    token: str


@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=201,
    summary="회원가입",
    description="이메일·비밀번호로 계정을 생성합니다. 이용약관 동의 필수. MAIL_USERNAME 설정 시 인증 이메일 발송.",
)
@limiter.limit("10/minute")
def register(request: Request, body: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == body.email).first():
        raise HTTPException(status_code=409, detail="이미 사용 중인 이메일입니다.")

    mail_enabled = bool(settings.MAIL_USERNAME)
    user = User(
        email=body.email,
        hashed_password=hash_password(body.password),
        nickname=body.nickname,
        terms_agreed_at=datetime.now(timezone.utc) if body.terms_agreed else None,
        is_verified=not mail_enabled,  # 이메일 미설정 시 자동 인증
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    email_sent = False
    if mail_enabled:
        token = secrets.token_urlsafe(32)
        ev = EmailVerificationToken(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
        db.add(ev)
        db.commit()
        from app.services.email_service import send_verification_email
        try:
            asyncio.run(send_verification_email(user.email, token, settings.FRONTEND_URL))
            email_sent = True
        except Exception:
            logger.warning("인증 이메일 발송 실패: user_id=%s", user.id, exc_info=True)

    logger.info("회원가입: user_id=%s (is_verified=%s, email_sent=%s)", user.id, user.is_verified, email_sent)
    result = RegisterResponse.model_validate(user)
    result.email_sent = email_sent
    return result


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="로그인",
    description="access_token(30분)과 refresh_token(7일)을 반환합니다.",
)
@limiter.limit("20/minute")
def login(request: Request, body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호가 올바르지 않습니다.")
    if not user.is_active:
        raise HTTPException(status_code=401, detail="비활성화된 계정입니다.")
    if settings.REQUIRE_EMAIL_VERIFICATION and not user.is_verified:
        raise HTTPException(status_code=403, detail="이메일 인증이 필요합니다. 가입 시 발송된 메일을 확인해 주세요.")

    access_token = create_access_token(user.id, user.email)
    refresh_token = create_refresh_token(user.id)
    user.refresh_token_hash = hash_token(refresh_token)
    user.tokens_valid_after = None  # 재로그인 시 이전 무효화 해제
    db.commit()

    logger.info("로그인: user_id=%s", user.id)
    return TokenResponse(access_token=access_token, refresh_token=refresh_token)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="토큰 재발급 (Rotation)",
    description="유효한 refresh_token으로 새 access_token과 refresh_token을 발급합니다. 기존 refresh_token은 즉시 무효화됩니다.",
)
@limiter.limit("30/minute")
def refresh(request: Request, body: RefreshRequest, db: Session = Depends(get_db)):
    payload = decode_token(body.refresh_token)
    if not payload or payload.get("type") != "refresh":
        raise HTTPException(status_code=401, detail="유효하지 않은 리프레시 토큰입니다.")

    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="유효하지 않은 계정입니다.")
    if user.refresh_token_hash != hash_token(body.refresh_token):
        raise HTTPException(status_code=401, detail="리프레시 토큰이 만료되었습니다. 다시 로그인해 주세요.")

    new_refresh_token = create_refresh_token(user.id)
    user.refresh_token_hash = hash_token(new_refresh_token)
    db.commit()

    return TokenResponse(
        access_token=create_access_token(user.id, user.email),
        refresh_token=new_refresh_token,
    )


@router.post(
    "/logout",
    status_code=204,
    summary="로그아웃",
    description="refresh_token 및 access_token을 즉시 무효화합니다. 클라이언트에서도 토큰을 삭제해야 합니다.",
)
@limiter.limit("20/minute")
def logout(request: Request, body: RefreshRequest, db: Session = Depends(get_db)):
    payload = decode_token(body.refresh_token)
    if not payload or payload.get("type") != "refresh":
        return  # 이미 만료된 토큰도 로그아웃 성공으로 처리

    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if user:
        user.refresh_token_hash = None
        user.tokens_valid_after = datetime.now(timezone.utc)
        db.commit()
        logger.info("로그아웃: user_id=%s", user.id)


@router.post(
    "/verify-email",
    status_code=204,
    summary="이메일 인증",
    description="회원가입 후 발송된 인증 링크의 token으로 이메일을 인증합니다.",
)
@limiter.limit("10/minute")
def verify_email(request: Request, body: VerifyEmailRequest, db: Session = Depends(get_db)):
    token_hash = hash_token(body.token)
    now = datetime.now(timezone.utc)

    ev = db.query(EmailVerificationToken).filter(
        EmailVerificationToken.token_hash == token_hash,
    ).first()

    if not ev or ev.used_at is not None or ev.expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=400, detail="유효하지 않거나 만료된 인증 링크입니다.")

    user = db.query(User).filter(User.id == ev.user_id, User.is_active.is_(True)).first()
    if not user:
        raise HTTPException(status_code=400, detail="유효하지 않은 요청입니다.")

    user.is_verified = True
    ev.used_at = now
    db.commit()
    logger.info("이메일 인증 완료: user_id=%s", user.id)


@router.post(
    "/resend-verification",
    status_code=204,
    summary="인증 이메일 재발송",
    description="인증되지 않은 계정에 인증 이메일을 다시 발송합니다.",
)
@limiter.limit("3/minute")
def resend_verification(request: Request, body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    if not settings.MAIL_USERNAME:
        return  # 이메일 미설정 환경에서는 no-op

    user = db.query(User).filter(User.email == body.email, User.is_active.is_(True)).first()
    if not user or user.is_verified:
        return  # 미존재·이미 인증된 계정 — 정보 노출 방지

    token = secrets.token_urlsafe(32)
    ev = EmailVerificationToken(
        user_id=user.id,
        token_hash=hash_token(token),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db.add(ev)
    db.commit()

    from app.services.email_service import send_verification_email
    try:
        asyncio.run(send_verification_email(user.email, token, settings.FRONTEND_URL))
    except Exception:
        logger.warning("인증 이메일 재발송 실패: user_id=%s", user.id, exc_info=True)


@router.post(
    "/forgot-password",
    status_code=204,
    summary="비밀번호 재설정 요청",
    description="이메일로 재설정 링크를 발송합니다. 이메일 존재 여부와 관계없이 204를 반환합니다.",
)
@limiter.limit("5/minute")
async def forgot_password(request: Request, body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email, User.is_active.is_(True)).first()
    if not user:
        return  # 이메일 존재 여부를 노출하지 않음

    token = secrets.token_urlsafe(32)
    token_hash = hash_token(token)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)

    reset = PasswordResetToken(user_id=user.id, token_hash=token_hash, expires_at=expires_at)
    db.add(reset)
    db.commit()

    from app.services.email_service import send_password_reset
    await send_password_reset(user.email, token, settings.FRONTEND_URL)
    logger.info("비밀번호 재설정 요청: user_id=%s", user.id)


@router.post(
    "/reset-password",
    status_code=204,
    summary="비밀번호 재설정",
    description="재설정 링크의 token과 새 비밀번호로 비밀번호를 변경합니다.",
)
@limiter.limit("10/minute")
def reset_password(request: Request, body: ResetPasswordRequest, db: Session = Depends(get_db)):
    token_hash = hash_token(body.token)
    now = datetime.now(timezone.utc)

    reset = db.query(PasswordResetToken).filter(
        PasswordResetToken.token_hash == token_hash,
    ).first()

    if not reset or reset.used_at is not None or reset.expires_at.replace(tzinfo=timezone.utc) < now:
        raise HTTPException(status_code=400, detail="유효하지 않거나 만료된 재설정 링크입니다.")

    user = db.query(User).filter(User.id == reset.user_id, User.is_active.is_(True)).first()
    if not user:
        raise HTTPException(status_code=400, detail="유효하지 않은 요청입니다.")

    user.hashed_password = hash_password(body.new_password)
    user.refresh_token_hash = None  # 기존 세션 무효화
    user.tokens_valid_after = now
    reset.used_at = now
    db.commit()
    logger.info("비밀번호 재설정 완료: user_id=%s", user.id)
