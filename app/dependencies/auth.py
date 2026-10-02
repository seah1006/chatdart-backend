import hmac
from datetime import timezone
from typing import Optional

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader, HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import decode_token
from app.db.database import get_db

def _check_token_revoked(payload: dict, user) -> None:
    """토큰 발급 시각(iat)이 user.tokens_valid_after보다 이전이면 무효화된 토큰으로 처리."""
    if not user.tokens_valid_after:
        return
    token_iat = payload.get("iat")
    if token_iat is None:
        raise HTTPException(status_code=401, detail="토큰이 무효화되었습니다. 다시 로그인해 주세요.")
    tvat = user.tokens_valid_after
    if tvat.tzinfo is None:
        tvat = tvat.replace(tzinfo=timezone.utc)
    # JWT iat는 정수(초 단위)이므로 tvat도 초 단위로 truncate해 비교한다.
    # <= 비교: 로그아웃과 같은 초에 발급된 이전 토큰도 무효화.
    # 재로그인 시 login()에서 tokens_valid_after=None으로 초기화하므로 새 토큰은 영향 없음.
    if token_iat <= int(tvat.timestamp()):
        raise HTTPException(status_code=401, detail="토큰이 무효화되었습니다. 다시 로그인해 주세요.")


# 스킴 인스턴스 — 함수 정의 전에 모두 선언
api_key_header = APIKeyHeader(name="X-API-Key")
_api_key_optional = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer_scheme = HTTPBearer(auto_error=False)


# ── 기존 X-API-Key 인증 (finance 엔드포인트 단독 사용 시) ────────────────────

async def verify_api_key(api_key: str = Security(api_key_header)):
    if not settings.API_SECRET_KEY:
        raise HTTPException(status_code=500, detail="서버에 API_SECRET_KEY가 설정되지 않았습니다.")
    if not hmac.compare_digest(api_key, settings.API_SECRET_KEY):
        raise HTTPException(status_code=403, detail="Invalid API Key")
    return api_key


# ── 통합 인증 (finance 엔드포인트용) ─────────────────────────────────────────

async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    api_key: Optional[str] = Security(_api_key_optional),
    db: Session = Depends(get_db),
):
    """JWT Bearer 또는 X-API-Key 중 하나로 인증.

    - JWT 제공 시: User 반환 → SearchLog.user_id 기록, 이메일 알림 활성화
    - X-API-Key 제공 시: None 반환 (익명 접근, 기존 동작 유지)
    - 둘 다 없거나 유효하지 않으면 401
    """
    from app.db.models import User  # 순환 import 방지

    if credentials:
        payload = decode_token(credentials.credentials)
        if not payload or payload.get("type") != "access":
            raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")
        user = db.query(User).filter(User.id == int(payload["sub"])).first()
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="유효하지 않은 계정입니다.")
        _check_token_revoked(payload, user)
        return user

    if api_key:
        if settings.API_SECRET_KEY and hmac.compare_digest(api_key, settings.API_SECRET_KEY):
            return None
        raise HTTPException(status_code=403, detail="Invalid API Key")

    raise HTTPException(status_code=401, detail="인증이 필요합니다.")


# ── JWT Bearer 인증 (회원 전용 엔드포인트용) ─────────────────────────────────

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
):
    """Authorization: Bearer <access_token> 헤더로 현재 유저를 반환."""
    from app.db.models import User  # 순환 import 방지

    if not credentials:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")

    payload = decode_token(credentials.credentials)
    if not payload or payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="유효하지 않은 토큰입니다.")

    user = db.query(User).filter(User.id == int(payload["sub"])).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="유효하지 않은 계정입니다.")
    _check_token_revoked(payload, user)
    return user


async def get_admin_user(current_user=Depends(get_current_user)):
    """관리자 권한 검증. get_current_user 통과 후 is_admin 확인."""
    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    return current_user
