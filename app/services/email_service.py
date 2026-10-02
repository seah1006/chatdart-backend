import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger(__name__)


async def send_email(to: str, subject: str, body: str) -> None:
    """이메일 발송. MAIL_USERNAME 미설정 시 조용히 스킵."""
    if not settings.MAIL_USERNAME or not settings.MAIL_PASSWORD:
        return
    try:
        await asyncio.to_thread(_send_sync, to, subject, body)
    except Exception as e:
        logger.warning("이메일 발송 실패 (to=%s): %s", to, e)


def _send_sync(to: str, subject: str, body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = settings.MAIL_FROM or settings.MAIL_USERNAME
    msg["To"] = to
    msg.attach(MIMEText(body, "plain", "utf-8"))
    with smtplib.SMTP(settings.MAIL_SERVER, settings.MAIL_PORT, timeout=10) as smtp:
        smtp.starttls()
        smtp.login(settings.MAIL_USERNAME, settings.MAIL_PASSWORD)
        smtp.send_message(msg)


async def send_collect_complete(email: str, company_name: str, stock_code: str) -> None:
    subject = f"[ChatDART] {company_name} 재무 데이터 수집 완료"
    body = (
        f"{company_name}({stock_code}) 재무 데이터 수집이 완료되었습니다.\n\n"
        "ChatDART에서 분석 결과를 확인하세요."
    )
    await send_email(email, subject, body)


async def send_verification_email(email: str, token: str, frontend_url: str) -> None:
    subject = "[ChatDART] 이메일 인증 안내"
    body = (
        f"ChatDART에 가입해 주셔서 감사합니다.\n\n"
        f"아래 링크를 클릭하여 이메일 인증을 완료해 주세요 (24시간 이내):\n\n"
        f"{frontend_url}/verify-email?token={token}\n\n"
        "본인이 가입하지 않은 경우 이 메일을 무시해 주세요."
    )
    await send_email(email, subject, body)


async def send_password_reset(email: str, token: str, frontend_url: str) -> None:
    subject = "[ChatDART] 비밀번호 재설정 안내"
    body = (
        f"비밀번호 재설정 링크입니다 (30분 이내 사용):\n\n"
        f"{frontend_url}/reset-password?token={token}\n\n"
        "본인이 요청하지 않은 경우 이 메일을 무시해 주세요."
    )
    await send_email(email, subject, body)


async def send_membership_expiry_warning(email: str, expires_at: str) -> None:
    subject = "[ChatDART] Pro 멤버십 만료 예정 안내"
    body = (
        f"Pro 멤버십이 {expires_at}에 만료될 예정입니다.\n\n"
        "ChatDART에서 구독을 연장하면 계속 이용할 수 있습니다."
    )
    await send_email(email, subject, body)
