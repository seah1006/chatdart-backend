from pydantic import BaseModel, Field, field_validator
from typing import Optional, List
from datetime import datetime


class UpdateUserRequest(BaseModel):
    nickname: Optional[str] = None
    current_password: Optional[str] = None
    new_password: Optional[str] = None

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        from app.schemas.auth_schema import _check_password_strength
        return _check_password_strength(v)


class UsageResponse(BaseModel):
    membership_tier: str
    watchlist_count: int
    watchlist_limit: int
    account_created_at: datetime


class WatchlistItem(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    stock_code: str
    company_name: str
    added_at: datetime
    memo: Optional[str] = None


class WatchlistSummaryItem(BaseModel):
    stock_code: str
    company_name: str
    fiscal_year: Optional[int] = None
    revenue: Optional[int] = None
    operating_profit: Optional[int] = None
    growth_status: Optional[str] = None
    profitability_status: Optional[str] = None
    stability_status: Optional[str] = None
    status: str


class DashboardPopularItem(BaseModel):
    stock_code: str
    company_name: str
    search_count: int


class DashboardRecentItem(BaseModel):
    stock_code: str
    company_name: str
    searched_at: datetime


class DashboardResponse(BaseModel):
    membership_tier: str
    watchlist_count: int
    watchlist_limit: int
    watchlist_preview: List[WatchlistSummaryItem]
    popular_week: List[DashboardPopularItem]
    recent_searches: List[DashboardRecentItem]


class WatchlistAddRequest(BaseModel):
    stock_code: str
    company_name: str


class WatchlistAddResponse(BaseModel):
    item: WatchlistItem
    watchlist_count: int
    watchlist_limit: int


class WatchlistUpdateRequest(BaseModel):
    memo: Optional[str] = Field(None, max_length=500)


class HistoryItem(BaseModel):
    model_config = {"from_attributes": True}

    stock_code: str
    company_name: str
    searched_at: datetime


class InquiryCreateRequest(BaseModel):
    title: str
    content: str


class InquiryResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    title: str
    content: str
    answer: Optional[str] = None
    status: str
    created_at: datetime
    answered_at: Optional[datetime] = None


class PaymentHistoryItem(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    plan: str
    amount: int
    payment_id: str
    status: str
    created_at: datetime
