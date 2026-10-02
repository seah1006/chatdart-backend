"""PG사(포트원·토스페이먼츠) 결제 검증 서비스.

PG_PROVIDER 환경변수로 사용할 PG를 선택합니다.
- 개발 환경: PG 키 미설정 시 검증 스킵 (경고 로그)
- 운영 환경(ENV=production): PG 키 미설정 시 503 오류
"""
import base64
import binascii
import hashlib
import hmac
import logging
import time
from datetime import datetime
from typing import Mapping, Optional

import httpx
from fastapi import HTTPException

from app.core.config import settings

logger = logging.getLogger(__name__)

_PORTONE_TOKEN_URL = "https://api.iamport.kr/users/getToken"
_PORTONE_PAYMENT_URL = "https://api.iamport.kr/payments/{imp_uid}"
_TOSS_PAYMENT_URL = "https://api.tosspayments.com/v1/payments/{payment_key}"
_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 5 * 60


def verify_webhook_signature(raw_body: bytes, headers: Mapping[str, str]) -> bool:
    """PG_PROVIDER에 맞는 webhook HMAC signature를 검증한다."""
    normalized_headers = {key.lower(): value for key, value in headers.items()}
    if settings.PG_PROVIDER == "portone":
        return _verify_portone_signature(raw_body, normalized_headers)
    if settings.PG_PROVIDER == "toss":
        return _verify_toss_signature(raw_body, normalized_headers)
    return False


def _verify_portone_signature(raw_body: bytes, headers: Mapping[str, str]) -> bool:
    if not settings.PORTONE_WEBHOOK_SECRET:
        return False

    webhook_id = headers.get("webhook-id")
    timestamp = headers.get("webhook-timestamp")
    signature_header = headers.get("webhook-signature")
    if not (webhook_id and timestamp and signature_header):
        return False

    if not _is_timestamp_fresh(timestamp):
        return False

    secret_b64 = settings.PORTONE_WEBHOOK_SECRET.removeprefix("whsec_")
    try:
        secret_bytes = base64.b64decode(secret_b64, validate=True)
    except (binascii.Error, ValueError):
        return False

    message = f"{webhook_id}.{timestamp}.".encode() + raw_body
    expected = hmac.new(secret_bytes, message, hashlib.sha256).digest()
    expected_b64 = base64.b64encode(expected).decode()

    for token in signature_header.split():
        if "," not in token:
            continue
        version, signature = token.split(",", 1)
        if version == "v1" and hmac.compare_digest(signature, expected_b64):
            return True
    return False


def _verify_toss_signature(raw_body: bytes, headers: Mapping[str, str]) -> bool:
    if not settings.TOSS_WEBHOOK_SECRET_KEY:
        return False

    signature = headers.get("tosspayments-webhook-signature")
    timestamp = headers.get("tosspayments-webhook-transmission-time")
    if not (signature and timestamp):
        return False

    if not _is_timestamp_fresh(timestamp):
        return False

    message = raw_body + f":{timestamp}".encode()
    expected = hmac.new(
        settings.TOSS_WEBHOOK_SECRET_KEY.encode(),
        message,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def _is_timestamp_fresh(timestamp_value: str) -> bool:
    try:
        if timestamp_value.isdigit():
            timestamp = int(timestamp_value)
        else:
            timestamp = int(datetime.fromisoformat(timestamp_value.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return False

    return abs(int(time.time()) - timestamp) <= _WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS


async def verify_payment(pg_transaction_id: str, expected_amount: int) -> bool:
    """PG사 API를 호출해 결제 금액·상태를 검증합니다.

    Returns:
        True  — 검증 성공 (금액 일치, 결제 완료 상태)
        False — 검증 실패
    """
    if settings.PG_PROVIDER == "portone":
        return await _verify_portone(pg_transaction_id, expected_amount)
    if settings.PG_PROVIDER == "toss":
        return await _verify_toss(pg_transaction_id, expected_amount)

    if settings.is_production:
        raise HTTPException(
            status_code=503,
            detail="결제 검증 서비스가 설정되지 않았습니다. 관리자에게 문의하세요.",
        )
    logger.warning(
        "PG_PROVIDER=%s — 지원하지 않는 PG 또는 미설정. 결제 검증 스킵 (개발 환경).",
        settings.PG_PROVIDER,
    )
    return True


# ── 포트원(아임포트) ──────────────────────────────────────────────────────────

async def _portone_get_token() -> Optional[str]:
    if not settings.PORTONE_API_KEY or not settings.PORTONE_API_SECRET:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _PORTONE_TOKEN_URL,
                json={"imp_key": settings.PORTONE_API_KEY, "imp_secret": settings.PORTONE_API_SECRET},
            )
            resp.raise_for_status()
            return resp.json()["response"]["access_token"]
    except Exception as e:
        logger.error("포트원 토큰 발급 실패: %s", e)
        return None


async def _verify_portone(imp_uid: str, expected_amount: int) -> bool:
    token = await _portone_get_token()
    if not token:
        if settings.is_production:
            raise HTTPException(
                status_code=503,
                detail="포트원 API 키가 설정되지 않았습니다. 관리자에게 문의하세요.",
            )
        logger.warning("포트원 API 키 미설정 — 결제 검증 스킵 (개발 환경)")
        return True

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _PORTONE_PAYMENT_URL.format(imp_uid=imp_uid),
                headers={"Authorization": token},
            )
            resp.raise_for_status()
            data = resp.json()["response"]

        if data["status"] != "paid":
            logger.warning("포트원 결제 상태 불일치: %s (imp_uid=%s)", data["status"], imp_uid)
            return False
        if data["amount"] != expected_amount:
            logger.warning(
                "포트원 결제 금액 불일치: 실제=%s 기대=%s (imp_uid=%s)",
                data["amount"], expected_amount, imp_uid,
            )
            return False
        return True
    except Exception as e:
        logger.error("포트원 결제 검증 실패 (imp_uid=%s): %s", imp_uid, e)
        return False


# ── 토스페이먼츠 ──────────────────────────────────────────────────────────────

async def _verify_toss(payment_key: str, expected_amount: int) -> bool:
    if not settings.TOSS_SECRET_KEY:
        if settings.is_production:
            raise HTTPException(
                status_code=503,
                detail="토스페이먼츠 시크릿 키가 설정되지 않았습니다. 관리자에게 문의하세요.",
            )
        logger.warning("TOSS_SECRET_KEY 미설정 — 결제 검증 스킵 (개발 환경)")
        return True

    encoded = base64.b64encode(f"{settings.TOSS_SECRET_KEY}:".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                _TOSS_PAYMENT_URL.format(payment_key=payment_key),
                headers={"Authorization": f"Basic {encoded}"},
            )
            resp.raise_for_status()
            data = resp.json()

        if data["status"] != "DONE":
            logger.warning("토스 결제 상태 불일치: %s (key=%s)", data["status"], payment_key)
            return False
        if data["totalAmount"] != expected_amount:
            logger.warning(
                "토스 결제 금액 불일치: 실제=%s 기대=%s (key=%s)",
                data["totalAmount"], expected_amount, payment_key,
            )
            return False
        return True
    except Exception as e:
        logger.error("토스 결제 검증 실패 (key=%s): %s", payment_key, e)
        return False
