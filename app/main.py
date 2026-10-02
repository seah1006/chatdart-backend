from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import sys
import threading

# ── Windows cp949 인코딩 보정 ────────────────────────────────────────────────
# dart-fss가 내부적으로 yaspin(브레일 스피너 문자 U+280B 등)을 사용하는데,
# Windows 기본 터미널 인코딩(cp949)은 이 문자를 지원하지 않아 UnicodeEncodeError 발생.
# Sentry threading 통합이 해당 예외를 main thread로 re-raise → 서버 크래시.
# 가장 앞에서 stdout/stderr를 UTF-8로 재설정해 yaspin 스레드가 안전하게 쓸 수 있도록 함.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass  # 파일로 리다이렉트된 경우 reconfigure 불가 — 무시

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from sqlalchemy import text, func as sa_func
from sqlalchemy.exc import OperationalError as SAOperationalError
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

from alembic.config import Config as AlembicConfig
from alembic import command as alembic_command

from app.core.config import settings
from app.core.logging import setup_logging
from app.db.database import SessionLocal
from app.db.models import SearchLog, UserMembership, User, CollectionTask
from app.services.dart_service import DartService
from app.services.forecast_service import MODEL_PATH as _AI_MODEL_PATH
from app.dependencies.rate_limit import limiter
from app.api.v1 import finance, auth, users, membership, notices, admin

# 구조화 로깅 설정 — production/staging은 JSON, development는 텍스트
setup_logging(
    log_level=settings.LOG_LEVEL,
    use_json=not settings.is_development,
)

import logging
_startup_logger = logging.getLogger(__name__)

# Sentry 초기화 — 운영/스테이징에서 SENTRY_DSN이 설정된 경우에만 활성화
if settings.SENTRY_DSN and not settings.is_development:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.ENV,
        release=settings.VERSION,
        integrations=[
            FastApiIntegration(),
            SqlalchemyIntegration(),
        ],
        traces_sample_rate=0.2,   # 요청의 20%만 성능 추적 (DART API 부하 고려)
        send_default_pii=False,   # 개인정보 전송 금지
    )

def _collect_popular_stocks(top_n: int) -> None:
    """SearchLog 상위 top_n개 종목 중 7일 이상 된 것을 재수집. 스레드 안전."""
    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=30)
        stale_cutoff = now - timedelta(days=7)
        stock_codes = [
            sc for (sc,) in (
                db.query(SearchLog.stock_code)
                .filter(SearchLog.searched_at >= since)
                .group_by(SearchLog.stock_code)
                .order_by(sa_func.count(SearchLog.stock_code).desc())
                .limit(top_n)
                .all()
            )
        ]
        tasks_by_code = {
            t.stock_code: t
            for t in db.query(CollectionTask)
            .filter(CollectionTask.stock_code.in_(stock_codes))
            .all()
        }
        for stock_code in stock_codes:
            task = tasks_by_code.get(stock_code)
            if task and task.status == "processing":
                continue
            if task and task.updated_at:
                updated = task.updated_at if task.updated_at.tzinfo else task.updated_at.replace(tzinfo=timezone.utc)
                if updated >= stale_cutoff:
                    continue
            if task:
                task.status = "processing"
                task.message = None
                task.started_at = now
            else:
                task = CollectionTask(stock_code=stock_code, status="processing", started_at=now)
                db.add(task)
            db.commit()
            _startup_logger.info("인기 종목 수집 시작: %s", stock_code)
            try:
                DartService.fetch_and_process_data(stock_code)
            except Exception as e:
                _startup_logger.warning("수집 실패 %s: %s", stock_code, e)
    except Exception as e:
        db.rollback()
        _startup_logger.warning("인기 종목 수집 오류: %s", e)
    finally:
        db.close()


def _prewarm_popular_stocks() -> None:
    """DART 초기화 완료 후 SearchLog 상위 5개 종목을 자동 수집 (서버 기동 1회)."""
    import time
    for _ in range(30):  # 최대 5분 대기
        if DartService._global_corp_list is not None or DartService._init_failed:
            break
        time.sleep(10)

    if DartService._init_failed or DartService._global_corp_list is None:
        _startup_logger.warning("DART 미준비 — 인기 종목 사전 수집 스킵")
        return

    _collect_popular_stocks(top_n=5)


def _prewarm_completed_induty_codes() -> None:
    """DART 준비 후 수집 완료 종목의 업종 코드만 백그라운드에서 캐시한다."""
    import time
    for _ in range(30):
        if DartService._global_corp_list is not None or DartService._init_failed:
            break
        time.sleep(10)
    if DartService._init_failed or DartService._global_corp_list is None:
        _startup_logger.warning("DART 미준비 — 완료 종목 업종 코드 프리웜 스킵")
        return

    db = None
    try:
        db = SessionLocal()
        stock_codes = [row.stock_code for row in db.query(CollectionTask.stock_code).filter(
            CollectionTask.status == "completed"
        ).all()]
        for stock_code in stock_codes:
            try:
                corp = DartService._find_corp(stock_code)
                if corp is not None:
                    DartService._fetch_induty_code(corp.corp_code)
            except Exception as e:
                _startup_logger.warning("업종 코드 프리웜 실패 [%s]: %s", stock_code, e)
    except Exception as e:
        _startup_logger.warning("완료 종목 업종 코드 프리웜 실패: %s", e)
    finally:
        if db is not None:
            db.close()


def _prewarm_krx_listed() -> None:
    """KRX 공식 API의 상장 종목 집합을 백그라운드에서 캐시한다."""
    try:
        DartService._fetch_krx_listed_codes()
    except Exception as e:
        _startup_logger.warning("KRX 상장 종목 프리웜 실패: %s", e)


def _prewarm_recommendation_cache() -> None:
    """DART 초기화 완료 후 recommendation 캐시를 미리 채운다."""
    import time
    for _ in range(30):
        if DartService._global_corp_list is not None or DartService._init_failed:
            break
        time.sleep(10)

    if DartService._init_failed or DartService._global_corp_list is None:
        _startup_logger.warning("DART 미준비 — recommendation 캐시 워밍 스킵")
        return

    from app.api.v1.finance import warm_recommendation_cache
    db = SessionLocal()
    try:
        warm_recommendation_cache(db, limits=[5, 10])
        _startup_logger.info("recommendation 캐시 워밍 완료 (limits=[5, 10])")
    except Exception as e:
        _startup_logger.warning("recommendation 캐시 워밍 실패: %s", e)
    finally:
        db.close()


def _next_kst_3am() -> datetime:
    """다음 KST 새벽 3시(= UTC 18:00)를 반환."""
    now = datetime.now(timezone.utc)
    target = now.replace(hour=18, minute=0, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return target


def _daily_prewarm_loop() -> None:
    """매일 KST 새벽 3시에 인기 종목 상위 10개를 재수집하는 데몬 루프."""
    import time
    while True:
        try:
            next_run = _next_kst_3am()
            sleep_secs = (next_run - datetime.now(timezone.utc)).total_seconds()
            kst_str = (next_run + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M KST")
            _startup_logger.info("다음 인기 종목 정기 수집 예약: %s (%.1f시간 후)", kst_str, sleep_secs / 3600)
            time.sleep(sleep_secs)
            _startup_logger.info("인기 종목 정기 수집 시작 (상위 10개)")
            _collect_popular_stocks(top_n=10)
        except Exception as e:
            _startup_logger.error("인기 종목 정기 수집 루프 오류 — 1분 후 재시도: %s", e, exc_info=True)
            time.sleep(60)


def _seed_admin_user(db) -> None:
    """ADMIN_EMAIL/ADMIN_PASSWORD가 설정된 경우 관리자 계정을 생성 또는 승격."""
    from app.core.security import hash_password
    from app.db.models import User

    if not settings.ADMIN_EMAIL or not settings.ADMIN_PASSWORD:
        return

    user = db.query(User).filter(User.email == settings.ADMIN_EMAIL).first()
    if user:
        if not user.is_admin:
            user.is_admin = True
            db.commit()
            _startup_logger.info("관리자 계정 승격 완료: user_id=%s", user.id)
        return

    admin = User(
        email=settings.ADMIN_EMAIL,
        hashed_password=hash_password(settings.ADMIN_PASSWORD),
        is_admin=True,
        is_active=True,
        is_verified=True,
    )
    db.add(admin)
    db.commit()
    _startup_logger.info("관리자 계정 생성 완료: user_id=%s", admin.id)


def _seed_demo_user(db) -> None:
    """DEMO_EMAIL/DEMO_PASSWORD가 설정된 경우 시연용 일반 계정을 생성."""
    from app.core.security import hash_password
    from app.db.models import User

    if not settings.DEMO_EMAIL or not settings.DEMO_PASSWORD:
        return

    existing = db.query(User).filter(User.email == settings.DEMO_EMAIL).first()
    if existing:
        existing.hashed_password = hash_password(settings.DEMO_PASSWORD)
        existing.is_active = True
        existing.is_verified = True
        db.commit()
        return

    demo = User(
        email=settings.DEMO_EMAIL,
        hashed_password=hash_password(settings.DEMO_PASSWORD),
        is_admin=False,
        is_active=True,
        is_verified=True,
    )
    db.add(demo)
    db.commit()
    _startup_logger.info("데모 계정 생성 완료: user_id=%s", demo.id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── DB 마이그레이션 ───────────────────────────────────────────────────────
    # Alembic을 도입하기 전에 SQLAlchemy create_all()로 생성된 DB는
    # alembic_version 테이블이 없어 "table already exists" 오류가 발생함.
    # 이 경우 현재 head로 stamp 처리해 이후 마이그레이션이 정상 동작하도록 복구.
    alembic_cfg = AlembicConfig("alembic.ini")
    # 앱이 이미 설정한 로깅(setup_logging)을 alembic env.py 의 fileConfig 가 덮지 않게 한다 (O84).
    alembic_cfg.attributes["configure_logger"] = False
    try:
        alembic_command.upgrade(alembic_cfg, "head")
    except SAOperationalError as e:
        if "already exists" in str(e):
            _startup_logger.warning(
                "Alembic 마이그레이션 충돌 감지: 테이블이 이미 존재합니다. "
                "alembic_version을 head로 stamp하고 계속 진행합니다. "
                f"(원인: {e})"
            )
            alembic_command.stamp(alembic_cfg, "head")
        elif settings.STRICT_MIGRATION:
            raise
        else:
            _startup_logger.critical(
                "Alembic migration failed (STRICT_MIGRATION=False); startup will continue. "
                "The DB schema may be out of date. Check /health. "
                f"(reason: {e})"
            )
    except Exception as e:
        if settings.STRICT_MIGRATION:
            raise
        _startup_logger.critical(
            "Alembic migration failed (STRICT_MIGRATION=False); startup will continue. "
            f"(reason: {e})"
        )

    # ── 관리자 계정 자동 Seed ───────────────────────────────────────────────────
    _db_seed = SessionLocal()
    try:
        _seed_admin_user(_db_seed)
    except Exception as e:
        _db_seed.rollback()
        _startup_logger.warning("관리자 계정 seed 실패: %s", e)
    finally:
        _db_seed.close()

    # ── 데모 계정 자동 Seed ───────────────────────────────────────────────────
    _db_demo_seed = SessionLocal()
    try:
        _seed_demo_user(_db_demo_seed)
    except Exception as e:
        _db_demo_seed.rollback()
        _startup_logger.warning("데모 계정 seed 실패: %s", e)
    finally:
        _db_demo_seed.close()

    # ── 서버 재시작 시 좀비 processing 태스크 복구 ───────────────────────────
    # 백그라운드 수집 중 서버가 종료되면 CollectionTask 상태가 processing으로 고착됨.
    # 재시작 시 일괄 failed 처리해 프론트가 재수집 요청을 할 수 있도록 복구.
    _db0 = SessionLocal()
    try:
        from app.db.models import CollectionTask
        recovered = _db0.query(CollectionTask).filter(
            CollectionTask.status == "processing"
        ).update({
            "status": "failed",
            "message": "서버 재시작으로 인해 수집이 중단되었습니다. 다시 수집을 요청해 주세요.",
        })
        _db0.commit()
        if recovered:
            _startup_logger.warning("좀비 processing 태스크 %d건 → failed 복구", recovered)
    except Exception as e:
        _db0.rollback()
        _startup_logger.warning("좀비 태스크 복구 실패: %s", e)
    finally:
        _db0.close()

    # ── SearchLog 30일 만료 데이터 정리 ──────────────────────────────────────
    # 인기 검색어 집계는 최신 데이터만 의미있으므로 30일 초과 로그를 서버 기동 시 삭제.
    _db = SessionLocal()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        deleted = _db.query(SearchLog).filter(SearchLog.searched_at < cutoff).delete()
        _db.commit()
        if deleted:
            _startup_logger.info("SearchLog 만료 데이터 %d건 삭제 (30일 기준)", deleted)
    except Exception as e:
        _db.rollback()
        _startup_logger.warning("SearchLog 정리 실패: %s", e)
    finally:
        _db.close()

    # ── CompareShare 만료 데이터 정리 ─────────────────────────────────────────
    from app.db.models import CompareShare
    _db_cs = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        deleted = _db_cs.query(CompareShare).filter(CompareShare.expires_at < now).delete()
        _db_cs.commit()
        if deleted:
            _startup_logger.info("CompareShare 만료 데이터 %d건 삭제", deleted)
    except Exception as e:
        _db_cs.rollback()
        _startup_logger.warning("CompareShare 정리 실패: %s", e)
    finally:
        _db_cs.close()

    # ── 만료된 멤버십 일괄 처리 ──────────────────────────────────────────────
    _db2 = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expired = (
            _db2.query(UserMembership)
            .filter(
                UserMembership.status.in_(["active", "cancelled"]),
                UserMembership.expires_at < now,
            )
            .all()
        )
        for m in expired:
            m.status = "expired"
            user = _db2.query(User).filter(User.id == m.user_id).first()
            if user:
                user.membership_tier = "free"
        if expired:
            _db2.commit()
            _startup_logger.info("만료 멤버십 %d건 처리 완료", len(expired))
    except Exception as e:
        _db2.rollback()
        _startup_logger.warning("만료 멤버십 처리 실패: %s", e)
    finally:
        _db2.close()

    # ── DART 기업 리스트 초기화 (백그라운드) ──────────────────────────────────
    # dart.get_corp_list()는 DART 서버에서 ~116,000개 기업 데이터를 다운로드하며
    # 약 1~2분 소요됨. lifespan에서 동기 호출하면 uvicorn이 그 시간 동안 요청을
    # 전혀 받지 못하므로 백그라운드 스레드로 분리.
    # 로딩 완료 전 요청은 DartService 내부에서 503으로 처리됨.
    t = threading.Thread(target=DartService.initialize, daemon=True, name="dart-init")
    t.start()
    _startup_logger.info("DART 기업 리스트 초기화 시작 (백그라운드). 서버는 즉시 요청을 수락합니다.")

    # DART 초기화 완료 후 인기 종목 자동 수집 (별도 스레드 — dart-init 완료를 내부에서 대기)
    if settings.ENABLE_BACKGROUND_PREWARM:
        threading.Thread(target=_prewarm_popular_stocks, daemon=True, name="prewarm").start()
        threading.Thread(
            target=_prewarm_completed_induty_codes, daemon=True, name="prewarm-induty"
        ).start()
        threading.Thread(
            target=_prewarm_krx_listed, daemon=True, name="prewarm-krx-listed"
        ).start()
        threading.Thread(
            target=_prewarm_recommendation_cache,
            daemon=True,
            name="prewarm-recommendation",
        ).start()

        # 매일 KST 새벽 3시 인기 종목 정기 수집
        threading.Thread(target=_daily_prewarm_loop, daemon=True, name="daily-prewarm").start()
    else:
        _startup_logger.info(
            "ENABLE_BACKGROUND_PREWARM=False; background prewarm threads skipped"
        )

    yield

_description = """
DART(전자공시시스템) 재무정보를 수집·분석하여 제공하는 REST API입니다.

## 인증

모든 `/api/v1/finance/*` 엔드포인트는 **`X-API-Key` 헤더**가 필요합니다.
우측 상단 **Authorize** 버튼을 클릭하여 API Key를 입력하면 이 페이지에서 직접 테스트할 수 있습니다.

## Rate Limit

| 엔드포인트 | 제한 |
|-----------|------|
| `POST /collect` | IP당 분당 5회 |
| `GET /search` | IP당 분당 30회 |

초과 시 **429 Too Many Requests** 반환.

## 금융업 종목 안내

은행·보험·증권 등 금융업 종목은 DART 공시 구조 차이로 인해
**총자산·부채·자기자본·당기순이익·현금**만 제공됩니다.
`source_report` 필드에 `[금융업: ...]` 문구가 포함됩니다.
"""

_tags_metadata = [
    {
        "name": "Finance",
        "description": "DART 재무정보 수집 및 분석. 모든 요청에 `X-API-Key` 헤더 필수.",
    },
    {
        "name": "System",
        "description": "서비스 상태 확인. 인증 불필요.",
    },
]

app = FastAPI(
    title=settings.PROJECT_NAME,
    version=settings.VERSION,
    description=_description,
    openapi_tags=_tags_metadata,
    lifespan=lifespan,
)

async def _rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """slowapi 기본 핸들러는 {"error": "..."} 포맷이므로, 다른 에러와 일관되게 {"detail": "..."} 로 통일."""
    return JSONResponse(
        status_code=429,
        content={"detail": f"요청 한도 초과: {exc.detail}. 잠시 후 다시 시도하세요."},
    )

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)

app.add_middleware(GZipMiddleware, minimum_size=1000)
def _build_cors_kwargs(settings_obj) -> dict:
    """운영에서는 FRONTEND_URL(+CORS_EXTRA_ORIGINS)만 허용, 개발에서만 ngrok·LAN 정규식 허용.

    staging은 production과 동일하게 명시 origin만 허용한다.
    CORS_EXTRA_ORIGINS는 콤마 구분 문자열이며 origin은 후행 슬래시 없이 등록한다.
    """
    extra = [o.strip() for o in settings_obj.CORS_EXTRA_ORIGINS.split(",") if o.strip()]
    kwargs = {
        "allow_origins": [settings_obj.FRONTEND_URL, *extra],
        "allow_credentials": True,
        "allow_methods": ["GET", "POST", "PATCH", "DELETE"],
        "allow_headers": ["*"],
        "expose_headers": ["X-Total-Count"],
    }
    if settings_obj.is_development:
        kwargs["allow_origins"] = [settings_obj.FRONTEND_URL, "http://localhost:3000", *extra]
        kwargs["allow_origin_regex"] = (
            r"https://.*\.ngrok-free\.(app|dev)"
            r"|https://.*\.ngrok\.io"
            r"|http://172\.16\.\d+\.\d+(:\d+)?"
            r"|http://192\.168\.\d+\.\d+(:\d+)?"
        )
    return kwargs


app.add_middleware(CORSMiddleware, **_build_cors_kwargs(settings))

# API 라우터 등록
app.include_router(finance.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(membership.router)
app.include_router(notices.router)
app.include_router(admin.router)


@app.get("/", include_in_schema=False)
def read_root():
    return {"message": f"{settings.PROJECT_NAME} is running perfectly!"}


@app.get(
    "/health",
    tags=["System"],
    summary="서비스 헬스 체크",
    description="DART 기업 리스트 로딩 상태와 DB 연결 상태를 반환합니다. 인증 불필요.",
    responses={
        200: {"description": "정상(`ok`) 또는 기동 중(`degraded`)"},
        429: {"description": "Rate Limit 초과 (IP당 분당 60회)"},
    },
)
@limiter.limit("60/minute")
def health_check(request: Request):
    # DART 기업 리스트 초기화 확인
    corp_list_ready = DartService._global_corp_list is not None
    init_failed = DartService._init_failed

    # DB 연결 확인
    db_ok = False
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        db_ok = True
    except Exception as e:
        _startup_logger.error(f"헬스체크 DB 연결 실패: {e}")

    status = "ok" if (corp_list_ready and db_ok and not init_failed) else "degraded"

    corp_list_status = "ready" if corp_list_ready else ("error" if init_failed else "loading")
    ai_model_ready = _AI_MODEL_PATH.exists()
    openai_configured = bool(settings.OPENAI_API_KEY)

    return {
        "status": status,
        "version": settings.VERSION,
        "corp_list": corp_list_status,
        "krx": (
            "ok" if DartService._krx_listed_codes is not None
            else ("unknown" if init_failed else "loading")
            if not DartService._krx_checked
            else ("degraded" if DartService._krx_degraded else "ok")
        ),
        "db": "ok" if db_ok else "error",
        "ai_model": "ready" if ai_model_ready else "missing",
        "openai": "configured" if openai_configured else "missing",
    }
