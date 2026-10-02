import hashlib
from datetime import datetime, timedelta, timezone
from threading import Lock

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AIAnalysisCache
from app.services.analysis import evaluate_growth, evaluate_stability

_counter_lock = Lock()
_counter_state = {"date": None, "count": 0}

# ── AI 해석(insights/interpret) 인메모리 캐시 (Phase 97) ─────────────────────
# 같은 flags 입력은 재사용한다. 재시작 시 유실 허용 (MVP, 단일 인스턴스).
_INTERPRET_CACHE_MAX = 256
_interpret_cache_lock = Lock()
_interpret_cache: dict = {}  # key -> (interpretation dict, expires_at)


def interpret_cache_key(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def get_interpret_cache(key: str):
    now = datetime.now(timezone.utc)
    with _interpret_cache_lock:
        entry = _interpret_cache.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at <= now:
            del _interpret_cache[key]
            return None
        return value


def set_interpret_cache(key: str, value: dict) -> None:
    now = datetime.now(timezone.utc)
    with _interpret_cache_lock:
        if len(_interpret_cache) >= _INTERPRET_CACHE_MAX:
            expired = [k for k, (_, exp) in _interpret_cache.items() if exp <= now]
            for k in expired:
                del _interpret_cache[k]
            if len(_interpret_cache) >= _INTERPRET_CACHE_MAX:
                oldest = min(_interpret_cache, key=lambda k: _interpret_cache[k][1])
                del _interpret_cache[oldest]
        _interpret_cache[key] = (value, now + timedelta(seconds=settings.AI_CACHE_TTL_SECONDS))


def check_daily_limit() -> None:
    """일일 한도 초과 시 HTTPException. AI_DAILY_LIMIT=0 이면 무제한."""
    if settings.AI_DAILY_LIMIT <= 0:
        return
    with _counter_lock:
        today = datetime.now(timezone.utc).date()
        if _counter_state["date"] != today:
            _counter_state["date"] = today
            _counter_state["count"] = 0
        if _counter_state["count"] >= settings.AI_DAILY_LIMIT:
            raise HTTPException(
                status_code=503,
                detail=f"오늘 AI 분석 호출 한도({settings.AI_DAILY_LIMIT}회)를 초과했습니다. 내일 다시 시도하세요.",
            )


def increment_daily_counter() -> None:
    with _counter_lock:
        today = datetime.now(timezone.utc).date()
        if _counter_state["date"] != today:
            _counter_state["date"] = today
            _counter_state["count"] = 0
        _counter_state["count"] += 1


def records_to_ai_input(records: list) -> dict:
    """최신순 5건을 AI팀 모델 입력 dict로 변환한다."""
    # ordered[0] is the oldest year and ordered[4] is the latest year.
    ordered = list(reversed(records))
    latest = records[0]
    previous = records[1]

    growth_label = evaluate_growth(latest, previous)
    stability_label = evaluate_stability(latest)
    trend = {"양호": "상승", "보합": "정체", "부진": "하락", "데이터 부족": "불명"}[growth_label]
    risk = {"우수": "낮음", "주의": "중간", "위험": "높음", "데이터 부족": "불명"}[stability_label]

    return {
        "company": latest.company_name or "",
        "trend": trend,
        "risk": risk,
        "revenue_0": float(ordered[0].revenue or 0),
        "revenue_1": float(ordered[1].revenue or 0),
        "revenue_2": float(ordered[2].revenue or 0),
        "revenue_3": float(ordered[3].revenue or 0),
        "revenue_4": float(ordered[4].revenue or 0),
        "op_0": float(ordered[0].operating_profit or 0),
        "op_1": float(ordered[1].operating_profit or 0),
        "op_2": float(ordered[2].operating_profit or 0),
        "op_3": float(ordered[3].operating_profit or 0),
        "op_4": float(ordered[4].operating_profit or 0),
        "net_0": float(ordered[0].net_income or 0),
        "net_1": float(ordered[1].net_income or 0),
        "net_2": float(ordered[2].net_income or 0),
        "net_3": float(ordered[3].net_income or 0),
        "net_4": float(ordered[4].net_income or 0),
        "debt_0": float(ordered[0].total_liabilities or 0),
        "debt_1": float(ordered[1].total_liabilities or 0),
        "debt_2": float(ordered[2].total_liabilities or 0),
        "debt_3": float(ordered[3].total_liabilities or 0),
        "debt_4": float(ordered[4].total_liabilities or 0),
        "equity_0": float(ordered[0].equity or 1),
        "equity_1": float(ordered[1].equity or 1),
        "equity_2": float(ordered[2].equity or 1),
        "equity_3": float(ordered[3].equity or 1),
        "equity_4": float(ordered[4].equity or 1),
        "cash_0": float(ordered[0].cash or 0),
        "cash_1": float(ordered[1].cash or 0),
        "cash_2": float(ordered[2].cash or 0),
        "cash_3": float(ordered[3].cash or 0),
        "cash_4": float(ordered[4].cash or 0),
    }


def records_to_fin_input(records: list) -> dict:
    """Convert latest records into financial model input (window=2, idx 0=old / 1=latest)."""
    from app.services.dart_service import DartService

    ordered = list(reversed(records))
    prev, latest = ordered[-2], ordered[-1]
    growth_label = evaluate_growth(records[0], records[1])
    stability_label = evaluate_stability(records[0])
    trend = {"양호": "상승", "보합": "정체", "부진": "하락", "데이터 부족": "불명"}[growth_label]
    risk = {"우수": "낮음", "주의": "중간", "위험": "높음", "데이터 부족": "불명"}[stability_label]

    def _ta(record):
        return float(record.total_assets or ((record.total_liabilities or 0) + (record.equity or 0)))

    return {
        "company": latest.company_name or "",
        "trend": trend,
        "risk": risk,
        "ta_0": _ta(prev),
        "ta_1": _ta(latest),
        "tl_0": float(prev.total_liabilities or 0),
        "tl_1": float(latest.total_liabilities or 0),
        "equity_0": float(prev.equity or 1),
        "equity_1": float(latest.equity or 1),
        "net_0": float(prev.net_income or 0),
        "net_1": float(latest.net_income or 0),
        "nii_0": float(prev.net_interest_income or 0),
        "nii_1": float(latest.net_interest_income or 0),
        "llp_0": float(prev.loan_loss_provision or 0),
        "llp_1": float(latest.loan_loss_provision or 0),
        "ins_liab_0": float(prev.insurance_liability or 0),
        "ins_liab_1": float(latest.insurance_liability or 0),
        "sector_detail": latest.sector_detail or DartService.classify_financial_sector(latest.company_id),
    }


def upsert_cache(
    db: Session,
    stock_code: str,
    fiscal_year: int,
    prediction: float,
    summary: str,
    now: datetime,
) -> None:
    expires_at = now + timedelta(seconds=settings.AI_CACHE_TTL_SECONDS)
    existing = (
        db.query(AIAnalysisCache)
        .filter(
            AIAnalysisCache.stock_code == stock_code,
            AIAnalysisCache.fiscal_year == fiscal_year,
        )
        .first()
    )
    if existing:
        existing.prediction = prediction
        existing.summary = summary
        existing.created_at = now
        existing.expires_at = expires_at
    else:
        db.add(AIAnalysisCache(
            stock_code=stock_code,
            fiscal_year=fiscal_year,
            prediction=prediction,
            summary=summary,
            created_at=now,
            expires_at=expires_at,
        ))
    db.commit()
