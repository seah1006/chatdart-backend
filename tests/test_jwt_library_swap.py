import jwt
import pytest
from datetime import datetime, timedelta, timezone
from app.core.security import decode_token
from app.core.config import settings

def test_decode_token_invalid_signature_returns_none():
    # 다른 시크릿으로 서명한 토큰
    wrong_secret = "wrong" * 10  # 32바이트 이상
    payload = {"sub": "123", "exp": datetime.now(timezone.utc) + timedelta(minutes=10)}
    bad_token = jwt.encode(payload, wrong_secret, algorithm=settings.JWT_ALGORITHM)
    
    assert decode_token(bad_token) is None

def test_decode_token_expired_returns_none():
    # 만료된 토큰
    payload = {
        "sub": "123",
        "exp": datetime.now(timezone.utc) - timedelta(minutes=1)
    }
    expired_token = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    
    assert decode_token(expired_token) is None

@pytest.mark.parametrize("malformed", [
    "not.a.jwt",
    "",
    "a.b",
    "a.b.c.d"
])
def test_decode_token_malformed_returns_none(malformed):
    assert decode_token(malformed) is None

def test_decode_token_wrong_algorithm_rejected():
    # algorithm="none"으로 서명한 토큰 (algorithm confusion 방어)
    payload = {"sub": "123", "exp": datetime.now(timezone.utc) + timedelta(minutes=10)}
    
    # PyJWT에서 none 알고리즘 사용 시 key는 None 또는 ""
    none_alg_token = jwt.encode(payload, key=None, algorithm="none")
    
    assert decode_token(none_alg_token) is None
