import string
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, EmailStr, field_validator

_PASSWORD_SPECIALS = set(string.punctuation)  # !"#$%&'()*+,-./:;<=>?@[\]^_`{|}~


def _check_password_strength(v: str) -> str:
    if len(v) < 10:
        raise ValueError("비밀번호는 10자 이상이어야 합니다.")
    if len(v.encode("utf-8")) > 72:
        raise ValueError("비밀번호는 72바이트를 초과할 수 없습니다.")
    if any(c.isspace() for c in v):
        raise ValueError("비밀번호에 공백을 포함할 수 없습니다.")
    if not any(c.isalpha() for c in v):
        raise ValueError("비밀번호에 글자를 포함해야 합니다.")
    if not any(c.isdigit() for c in v):
        raise ValueError("비밀번호에 숫자를 포함해야 합니다.")
    if not any(c in _PASSWORD_SPECIALS for c in v):
        raise ValueError("비밀번호에 특수문자를 포함해야 합니다.")
    return v


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    nickname: Optional[str] = None
    terms_agreed: bool

    @field_validator("password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _check_password_strength(v)

    @field_validator("terms_agreed")
    @classmethod
    def must_agree_terms(cls, v: bool) -> bool:
        if not v:
            raise ValueError("이용약관에 동의해야 합니다.")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def validate_password(cls, v: str) -> str:
        return _check_password_strength(v)


class UserResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    email: str
    nickname: Optional[str] = None
    membership_tier: str
    is_active: bool
    is_admin: bool
    is_verified: bool
    terms_agreed_at: Optional[datetime] = None
    created_at: datetime


class RegisterResponse(UserResponse):
    """회원가입 응답 — UserResponse + 이메일 발송 여부."""
    email_sent: bool = False
