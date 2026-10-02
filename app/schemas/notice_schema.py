from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime


class NoticeCreateRequest(BaseModel):
    title: str
    content: str
    is_pinned: bool = False


class NoticeUpdateRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    is_pinned: Optional[bool] = None


class NoticeResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    title: str
    content: str
    is_pinned: bool
    created_at: datetime
    updated_at: datetime


class NoticeListResponse(BaseModel):
    items: List[NoticeResponse]
    total: int
    page: int
    size: int
