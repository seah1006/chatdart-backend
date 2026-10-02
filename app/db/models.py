from sqlalchemy import (
    Column, Integer, String, DateTime, BigInteger, UniqueConstraint,
    Boolean, Text, ForeignKey, Float, Index,
)
from sqlalchemy.sql import func
from app.db.database import Base


class FinancialStatement(Base):
    __tablename__ = "Financial_Statement"

    __table_args__ = (
        UniqueConstraint('company_id', 'fiscal_year', name='uq_company_fiscal_year'),
    )

    fs_id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    company_id = Column(String(20), nullable=False, index=True)
    company_name = Column(String(200), nullable=True)
    fiscal_year = Column(Integer, nullable=False)

    revenue = Column(BigInteger)  # default=0 금지 — 명시적 None을 미수집 0으로 덮어쓴다.
    cost_of_sales = Column(BigInteger)  # default=0 금지 — 명시적 None을 미수집 0으로 덮어쓴다.
    gross_profit = Column(BigInteger)  # default=0 금지 — 명시적 None을 미수집 0으로 덮어쓴다.
    sga = Column(BigInteger)  # default=0 금지 — 명시적 None을 미수집 0으로 덮어쓴다.
    operating_profit = Column(BigInteger)  # default=0 금지 — 명시적 None을 미수집 0으로 덮어쓴다.
    net_income = Column(BigInteger, default=0)
    total_assets = Column(BigInteger, default=0)
    total_liabilities = Column(BigInteger, default=0)
    equity = Column(BigInteger, default=0)
    cash = Column(BigInteger, default=0)
    # 손익 세부 항목 (Phase 96) — 미수집 시 None. default=0 금지.
    other_income = Column(BigInteger, nullable=True)         # 기타수익
    other_expense = Column(BigInteger, nullable=True)        # 기타비용
    finance_income = Column(BigInteger, nullable=True)       # 금융수익
    finance_cost = Column(BigInteger, nullable=True)         # 금융비용
    income_before_tax = Column(BigInteger, nullable=True)    # 법인세비용차감전순이익
    income_tax_expense = Column(BigInteger, nullable=True)   # 법인세비용
    operating_cash_flow = Column(BigInteger, nullable=True)  # 영업활동현금흐름
    # Financial-sector fields. Non-financial rows leave these as None.
    net_interest_income = Column(BigInteger, nullable=True)
    loan_loss_provision = Column(BigInteger, nullable=True)
    insurance_liability = Column(BigInteger, nullable=True)
    sector_detail = Column(String(20), nullable=True)

    source_report = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())


class CollectionTask(Base):
    __tablename__ = "collection_tasks"
    stock_code = Column(String, primary_key=True, index=True)
    status = Column(String, default="pending")
    message = Column(String, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), default=func.now())


class SearchLog(Base):
    """기업 조회 로그 — /summary, /compare 호출 시 기록. 인기 종목 집계에 활용."""
    __tablename__ = "search_logs"
    __table_args__ = (
        Index("ix_search_logs_searched_at_stock_code", "searched_at", "stock_code"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    stock_code = Column(String(20), nullable=False, index=True)
    company_name = Column(String(200), nullable=False)
    searched_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)


# ── 회원 ──────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(255), nullable=False)
    nickname = Column(String(50), nullable=True)
    membership_tier = Column(String(20), nullable=False, default="free")
    is_active = Column(Boolean, nullable=False, default=True)
    is_admin = Column(Boolean, nullable=False, default=False)
    is_verified = Column(Boolean, nullable=False, default=False)
    terms_agreed_at = Column(DateTime(timezone=True), nullable=True)
    refresh_token_hash = Column(String(64), nullable=True)   # sha256 hex
    tokens_valid_after = Column(DateTime(timezone=True), nullable=True)  # 이 시각 이전 발급 토큰 무효
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())


# ── 즐겨찾기 ──────────────────────────────────────────────────────────────────

class Watchlist(Base):
    __tablename__ = "watchlist"

    __table_args__ = (
        UniqueConstraint("user_id", "stock_code", name="uq_watchlist_user_stock"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    stock_code = Column(String(20), nullable=False)
    company_name = Column(String(200), nullable=False)
    added_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    memo = Column(String(500), nullable=True)


# ── 멤버십 ────────────────────────────────────────────────────────────────────

class UserMembership(Base):
    __tablename__ = "user_memberships"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan = Column(String(20), nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String(20), nullable=False, default="active")  # active / expired / cancelled
    warning_sent = Column(Boolean, nullable=False, default=False)


class PaymentRecord(Base):
    __tablename__ = "payment_records"
    __table_args__ = (
        UniqueConstraint("payment_id", name="uq_payment_records_payment_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan = Column(String(20), nullable=False)
    amount = Column(Integer, nullable=False)
    payment_id = Column(String(50), unique=True, nullable=False, index=True)  # 내부 생성 ID
    pg_transaction_id = Column(String(255), nullable=True, unique=True)        # PG사 ID
    status = Column(String(20), nullable=False, default="pending")  # pending / paid / failed / refunded
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())


class CompareShare(Base):
    """비교 결과 공유 링크 — 익명 접근, 30일 후 자동 삭제."""
    __tablename__ = "compare_shares"

    share_id = Column(String(16), primary_key=True, index=True)
    payload = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)


class AIAnalysisCache(Base):
    __tablename__ = "ai_analysis_cache"

    __table_args__ = (
        UniqueConstraint("stock_code", "fiscal_year", name="uq_ai_cache_stock_fy"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    stock_code = Column(String(10), nullable=False)
    fiscal_year = Column(Integer, nullable=False)
    prediction = Column(Float, nullable=False)
    summary = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)


# ── 공지사항 / 문의 ───────────────────────────────────────────────────────────

class Notice(Base):
    __tablename__ = "notices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    is_pinned = Column(Boolean, nullable=False, default=False)
    author_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, default=func.now(), onupdate=func.now())


class Inquiry(Base):
    __tablename__ = "inquiries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(200), nullable=False)
    content = Column(Text, nullable=False)
    answer = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="pending")  # pending / answered
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
    answered_at = Column(DateTime(timezone=True), nullable=True)


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True)  # sha256 hex
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, unique=True)  # sha256 hex
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=func.now())
