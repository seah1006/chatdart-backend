from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", hide_input_in_errors=True)

    PROJECT_NAME: str = "ChatDART API"
    VERSION: str = "16.0.0"
    ENV: str = "development"          # development | staging | production

    DART_API_KEY: str
    # KRX 공식 API 키. 선택 항목 — 미설정이면 기존 DART 폴백 경로를 그대로 쓴다.
    # 기본값을 비워 두어야 키가 없는 환경(CI·다른 머신)에서도 기동한다.
    KRX_API_KEY: str = ""
    DATABASE_URL: str = "sqlite:///./chatdart.db"
    FRONTEND_URL: str = "http://localhost:3000"
    # 콤마 구분 추가 허용 CORS origin (scheme·port 포함, 예: http://118.36.70.42:3000)
    CORS_EXTRA_ORIGINS: str = ""
    API_SECRET_KEY: str = ""

    @field_validator("API_SECRET_KEY")
    @classmethod
    def api_secret_key_must_not_be_empty(cls, v: str) -> str:
        if not v:
            raise ValueError(
                "API_SECRET_KEY가 설정되지 않았습니다. "
                ".env에 API_SECRET_KEY=<32바이트 이상 랜덤값>을 설정하세요.\n"
                "생성 명령: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return v

    # 환경별 동작 제어
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"

    # DART 수집 재시도 대기 (초) — 쉼표 구분, 횟수 = 원소 수
    DART_RETRY_DELAYS: str = "5,30,120"

    # Sentry — 미설정 시 비활성화 (로컬 개발 환경에서는 불필요)
    SENTRY_DSN: str = ""

    # AI analysis. When unset, /summary/ai-analysis returns 503.
    OPENAI_API_KEY: str = ""
    AI_DAILY_LIMIT: int = 100
    AI_CACHE_TTL_SECONDS: int = 86_400
    STRICT_MIGRATION: bool = True
    ENABLE_BACKGROUND_PREWARM: bool = True

    # 관리자 계정 자동 생성 - 미설정 시 스킵
    ADMIN_EMAIL: str = ""
    ADMIN_PASSWORD: str = ""
    DEMO_EMAIL: str = ""
    DEMO_PASSWORD: str = ""

    # JWT — 회원 인증
    # 생성: python -c "import secrets; print(secrets.token_hex(32))"
    JWT_SECRET_KEY: str = ""

    @field_validator("JWT_SECRET_KEY")
    @classmethod
    def jwt_secret_key_must_not_be_empty(cls, v: str) -> str:
        if not v:
            raise ValueError(
                "JWT_SECRET_KEY가 설정되지 않았습니다. "
                ".env에 JWT_SECRET_KEY=<32바이트 이상 랜덤값>을 설정하세요.\n"
                "생성 명령: python -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return v
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # 이메일 알림 — 미설정 시 발송 비활성화
    REQUIRE_EMAIL_VERIFICATION: bool = False  # True 시 미인증 계정 로그인 차단
    MAIL_USERNAME: str = ""   # Gmail 계정 또는 SMTP 사용자명
    MAIL_PASSWORD: str = ""   # 앱 비밀번호 (Gmail 2FA 사용 시)
    MAIL_FROM: str = ""        # 발신자 주소 (미설정 시 MAIL_USERNAME 사용)
    MAIL_SERVER: str = "smtp.gmail.com"
    MAIL_PORT: int = 587

    # 결제 PG 설정 — 미설정 시 웹훅 서명 검증 스킵 (개발용)
    # PG_PROVIDER: "portone" 또는 "toss"
    PG_PROVIDER: str = "portone"
    PORTONE_API_KEY: str = ""     # 포트원 REST API 키
    PORTONE_API_SECRET: str = ""  # 포트원 REST API 시크릿
    TOSS_SECRET_KEY: str = ""     # 토스페이먼츠 시크릿 키
    PORTONE_WEBHOOK_SECRET: str = ""    # 포트원 V2 Webhook Secret (whsec_<base64>)
    TOSS_WEBHOOK_SECRET_KEY: str = ""   # 토스페이먼츠 Webhook Security Key
    WEBHOOK_SECRET: str = ""      # MVP/수동 웹훅 shared secret (X-Webhook-Secret)

    @model_validator(mode="after")
    def production_payment_secrets_must_not_be_empty(self) -> "Settings":
        required = {}
        if self.ENV != "development":
            if self.PG_PROVIDER == "portone":
                required = {
                    "PORTONE_API_KEY": self.PORTONE_API_KEY,
                    "PORTONE_API_SECRET": self.PORTONE_API_SECRET,
                    "PORTONE_WEBHOOK_SECRET": self.PORTONE_WEBHOOK_SECRET,
                }
            elif self.PG_PROVIDER == "toss":
                required = {
                    "TOSS_SECRET_KEY": self.TOSS_SECRET_KEY,
                    "TOSS_WEBHOOK_SECRET_KEY": self.TOSS_WEBHOOK_SECRET_KEY,
                }
        if self.ENV in {"production", "staging"}:
            required[
                "SENTRY_DSN (production/staging requires Sentry DSN for observability)"
            ] = self.SENTRY_DSN
        if self.REQUIRE_EMAIL_VERIFICATION:
            required[
                "MAIL_USERNAME (REQUIRE_EMAIL_VERIFICATION=True requires mail credentials)"
            ] = self.MAIL_USERNAME
            required[
                "MAIL_PASSWORD (REQUIRE_EMAIL_VERIFICATION=True requires mail credentials)"
            ] = self.MAIL_PASSWORD
        missing = [name for name, value in required.items() if value == ""]
        if missing:
            missing_names = ", ".join(missing)
            raise ValueError(
                f"ENV={self.ENV} requires non-empty payment secrets: {missing_names}. "
                "Set the PG API and webhook secrets in .env before deployment."
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @property
    def is_development(self) -> bool:
        return self.ENV == "development"


@lru_cache
def get_settings() -> Settings:
    """설정 객체를 캐싱하여 반환 — 테스트 시 override 가능"""
    return Settings()


settings = get_settings()
