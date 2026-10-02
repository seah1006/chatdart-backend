from pydantic import BaseModel
from typing import Optional
from datetime import datetime


class PlanInfo(BaseModel):
    plan: str
    name: str
    price: int
    duration_days: int


class MembershipResponse(BaseModel):
    model_config = {"from_attributes": True}

    plan: str
    started_at: datetime
    expires_at: Optional[datetime] = None
    status: str
    days_until_expiry: Optional[int] = None
    watchlist_count: int
    watchlist_limit: int


class SubscribeRequest(BaseModel):
    plan: str


class SubscribeResponse(BaseModel):
    payment_id: str
    amount: int
    plan: str


class WebhookRequest(BaseModel):
    payment_id: str
    pg_transaction_id: str
    status: str   # paid / failed
    amount: int


class CancelResponse(BaseModel):
    status: str
    message: str
