"""Seed ChatDART demo data into the configured database."""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

DEMO_EMAIL = os.getenv("DEMO_EMAIL", "demo@example.com")
DEMO_PASSWORD = os.getenv("DEMO_PASSWORD", "change-me-demo")

DEMO_STOCKS = [
    ("005930", "삼성전자", "반도체"),
    ("000660", "SK하이닉스", "반도체"),
    ("000990", "DB하이텍", "반도체"),
    ("005380", "현대차", "자동차"),
    ("000270", "기아", "자동차"),
    ("012330", "현대모비스", "자동차"),
    ("105560", "KB금융", "금융업"),
    ("066570", "LG전자", "전자"),
    ("035420", "NAVER", "인터넷"),
    ("035720", "카카오", "인터넷"),
    ("373220", "LG에너지솔루션", "2차전지"),
    ("207940", "삼성바이오로직스", "바이오"),
    ("068270", "셀트리온", "바이오"),
    ("005490", "POSCO홀딩스", "철강"),
    ("006400", "삼성SDI", "2차전지"),
]
WATCHLIST_CODES = ["005930", "005380", "000660"]
AI_WARM_CODES = [code for code, _name, _group in DEMO_STOCKS]

logger = logging.getLogger("seed_demo")
SessionLocal = None


class _DartServiceProxy:
    def initialize(self):
        from app.services.dart_service import DartService as _DartService

        return _DartService.initialize()

    def fetch_and_process_data(self, stock_code: str):
        from app.services.dart_service import DartService as _DartService

        return _DartService.fetch_and_process_data(stock_code)

    def __getattr__(self, name: str):
        from app.services.dart_service import DartService as _DartService

        return getattr(_DartService, name)


DartService = _DartServiceProxy()


def _read_dotenv_value(name: str) -> str:
    env_path = ROOT_DIR / ".env"
    if not env_path.exists():
        return ""
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip('"').strip("'")
    return ""


def _config_value(name: str) -> str:
    return os.getenv(name, "").strip() or _read_dotenv_value(name).strip()


def _require_dart_api_key() -> bool:
    if _config_value("DART_API_KEY"):
        return True
    print(
        "DART_API_KEY가 설정되지 않았습니다. "
        "환경변수 또는 .env에 DART_API_KEY를 설정한 뒤 다시 실행하세요.",
        file=sys.stderr,
    )
    return False


def initialize_dart() -> bool:
    logger.info("DART 기업 리스트 초기화를 시작합니다. 보통 1~2분 걸릴 수 있습니다.")
    DartService.initialize()
    if DartService._init_failed or DartService._global_corp_list is None:
        logger.error("DART 기업 리스트 초기화에 실패했습니다.")
        return False
    logger.info("DART 기업 리스트 초기화 완료: 상장사 %d개", len(DartService._stock_corps))
    return True


def _dart_company_name(stock_code: str, fallback: str) -> str:
    corp = DartService._global_corp_list.find_by_stock_code(stock_code)
    if corp is None:
        logger.warning("DART 기업 리스트에서 %s(%s)를 찾지 못했습니다.", fallback, stock_code)
        return fallback
    if corp.corp_name != fallback:
        logger.info("DART 기준 종목명 확인: %s %s -> %s", stock_code, fallback, corp.corp_name)
    return corp.corp_name


def _collect_one(
    code: str,
    display_name: str,
    group: str,
    index: int,
    total: int,
    force: bool = False,
) -> tuple[str, str, bool, str | None]:
    from app.db.models import CollectionTask, FinancialStatement

    global SessionLocal
    if SessionLocal is None:
        from app.db.database import SessionLocal as _SessionLocal

        SessionLocal = _SessionLocal

    db = SessionLocal()
    try:
        company_name = _dart_company_name(code, display_name)
        task = db.query(CollectionTask).filter(CollectionTask.stock_code == code).first()
        if not force and task and task.status == "completed":
            logger.info("[%d/%d] %s %s - 이미 completed 상태라 스킵", index, total, code, company_name)
            return code, company_name, True, None

        now = datetime.now(timezone.utc)
        if task is None:
            task = CollectionTask(
                stock_code=code,
                status="processing",
                message="demo seed collection",
                started_at=now,
                updated_at=now,
            )
            db.add(task)
        else:
            task.status = "processing"
            task.message = "demo seed collection"
            task.started_at = now
            task.updated_at = now
        db.commit()

        logger.info("[%d/%d] %s %s (%s) 수집 시작", index, total, code, company_name, group)
        try:
            DartService.fetch_and_process_data(code)
        except Exception as exc:
            logger.warning("[%d/%d] %s 수집 호출 실패: %s", index, total, code, exc)

        db.expire_all()
        task = db.query(CollectionTask).filter(CollectionTask.stock_code == code).first()
        row_count = db.query(FinancialStatement).filter(FinancialStatement.company_id == code).count()
        if task and task.status == "completed" and row_count > 0:
            logger.info("[%d/%d] %s 수집 성공: FinancialStatement %d행", index, total, code, row_count)
            return code, company_name, True, None

        reason = (task.message if task else "CollectionTask missing") or "unknown"
        logger.warning("[%d/%d] %s 수집 실패: %s", index, total, code, reason)
        return code, company_name, False, reason
    finally:
        db.close()


def collect_demo_stocks(db, concurrency: int = 3, force: bool = False):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    global SessionLocal
    if SessionLocal is None:
        from app.db.database import SessionLocal as _SessionLocal

        SessionLocal = _SessionLocal

    successful: list[tuple[str, str]] = []
    failed: list[tuple[str, str, str]] = []
    total = len(DEMO_STOCKS)
    max_workers = min(3, max(1, concurrency))

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_collect_one, code, display_name, group, index, total, force)
            for index, (code, display_name, group) in enumerate(DEMO_STOCKS, start=1)
        ]
        for future in as_completed(futures):
            code, company_name, ok, reason = future.result()
            if ok:
                successful.append((code, company_name))
            else:
                failed.append((code, company_name, reason or "unknown"))

    return successful, failed


def ensure_demo_user(db, email: str = DEMO_EMAIL, password: str = DEMO_PASSWORD):
    from app.core.security import hash_password
    from app.db.models import User

    now = datetime.now(timezone.utc)
    user = db.query(User).filter(User.email == email).first()
    if user:
        user.hashed_password = hash_password(password)
        user.is_active = True
        user.is_verified = True
        user.membership_tier = "free"
        if user.terms_agreed_at is None:
            user.terms_agreed_at = now
        db.commit()
        db.refresh(user)
        return user, "existing_reset_password"

    user = User(
        email=email,
        hashed_password=hash_password(password),
        membership_tier="free",
        is_active=True,
        is_verified=True,
        is_admin=False,
        terms_agreed_at=now,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user, "created"


def seed_legacy_skip_collect_data(db, multiplier: int) -> tuple[object, str, int]:
    from app.db.models import SearchLog, Watchlist

    legacy_stocks = [
        ("005930", "삼성전자"),
        ("000660", "SK하이닉스"),
        ("373220", "LG에너지솔루션"),
        ("207940", "삼성바이오로직스"),
        ("005490", "POSCO홀딩스"),
        ("035420", "NAVER"),
        ("005380", "현대차"),
        ("000270", "기아"),
        ("068270", "셀트리온"),
        ("035720", "카카오"),
    ]
    user, user_status = ensure_demo_user(db, "demo@chatdart.local", DEMO_PASSWORD)

    existing_codes = {
        row.stock_code
        for row in db.query(Watchlist).filter(Watchlist.user_id == user.id).all()
    }
    name_map = dict(legacy_stocks)
    for code in ["005930", "000660", "035420", "035720", "005380"]:
        if code not in existing_codes:
            db.add(Watchlist(user_id=user.id, stock_code=code, company_name=name_map[code]))
            existing_codes.add(code)

    db.query(SearchLog).filter(SearchLog.user_id == user.id).delete()
    now = datetime.now(timezone.utc)
    for code, name in legacy_stocks:
        for _ in range(multiplier):
            db.add(SearchLog(stock_code=code, company_name=name, searched_at=now, user_id=user.id))
    db.commit()
    watchlist_count = db.query(Watchlist).filter(Watchlist.user_id == user.id).count()
    return user, user_status, watchlist_count


def ensure_watchlist(db, user, successful: list[tuple[str, str]]) -> int:
    from app.db.models import Watchlist

    successful_names = dict(successful)
    existing_codes = {
        row.stock_code
        for row in db.query(Watchlist).filter(Watchlist.user_id == user.id).all()
    }
    for code in WATCHLIST_CODES:
        if code in existing_codes or code not in successful_names:
            continue
        db.add(
            Watchlist(
                user_id=user.id,
                stock_code=code,
                company_name=successful_names[code],
            )
        )
        existing_codes.add(code)
    db.commit()
    return db.query(Watchlist).filter(Watchlist.user_id == user.id).count()


def warm_ai_cache(db, user, enabled: bool) -> tuple[bool, str]:
    if not enabled:
        return False, "--warm-ai 미지정"
    if not _config_value("OPENAI_API_KEY"):
        return False, "OPENAI_API_KEY 미설정"

    from app.services.forecast_service import MODEL_PATH, MODEL_PATH_FIN

    missing = [str(path) for path in (Path(MODEL_PATH), Path(MODEL_PATH_FIN)) if not path.exists()]
    if missing:
        return False, f"모델 파일 없음: {', '.join(missing)}"

    from starlette.requests import Request

    from app.api.v1.finance import get_ai_analysis

    warmed: list[str] = []
    failed: list[str] = []
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/finance/summary/ai-analysis",
        "headers": [],
        "query_string": b"",
        "client": ("seed_demo", 0),
        "server": ("seed_demo", 0),
        "scheme": "http",
    }
    for code in AI_WARM_CODES:
        try:
            response = get_ai_analysis(Request(scope), keyword=code, db=db, current_user=user)
            if getattr(response, "summary_available", False):
                warmed.append(code)
            else:
                failed.append(code)
                logger.warning("AI 워밍 실패: %s - summary_available=False", code)
        except Exception as exc:
            failed.append(code)
            logger.warning("AI 워밍 실패: %s - %s", code, exc)

    detail = f"{len(warmed)}종목 워밍"
    if failed:
        detail += f", 실패 {len(failed)}: {', '.join(failed)}"
    return bool(warmed), detail


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed ChatDART demo data")
    parser.add_argument("--warm-ai", action="store_true", help="Warm AIAnalysisCache when OPENAI_API_KEY and model exist")
    parser.add_argument("--concurrency", type=int, default=3, help="Concurrent stock collection workers, capped at 3")
    parser.add_argument("--force", action="store_true", help="Force recollect completed stocks")
    parser.add_argument("--skip-collect", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--searchlog-multiplier", type=int, default=3, help=argparse.SUPPRESS)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.skip_collect and not _require_dart_api_key():
        return 1

    global SessionLocal
    if SessionLocal is None:
        from app.db.database import SessionLocal as _SessionLocal

        SessionLocal = _SessionLocal

    if not args.skip_collect and not initialize_dart():
        return 1

    db = SessionLocal()
    try:
        if args.skip_collect:
            successful = []
            failed = []
            user, user_status, watchlist_count = seed_legacy_skip_collect_data(db, args.searchlog_multiplier)
            ai_warmed, ai_status = False, "--skip-collect"
        else:
            successful, failed = collect_demo_stocks(db, concurrency=args.concurrency, force=args.force)
            user, user_status = ensure_demo_user(db)
            watchlist_count = ensure_watchlist(db, user, successful)
            ai_warmed, ai_status = warm_ai_cache(db, user, args.warm_ai)
    finally:
        db.close()

    print()
    print("=== ChatDART demo seed summary ===")
    print(f"collection_success: {len(successful)} - {', '.join(f'{code} {name}' for code, name in successful) or 'none'}")
    print(f"collection_failed: {len(failed)} - {', '.join(f'{code} {name}' for code, name, _ in failed) or 'none'}")
    if failed:
        for code, name, reason in failed:
            print(f"  - {code} {name}: {reason}")
    print(f"demo_user: {user.email} ({user_status}, password reset to configured demo password)")
    print(f"watchlist_count: {watchlist_count}")
    print(f"ai_warm: {'done' if ai_warmed else 'skipped'} ({ai_status})")
    return 0 if args.skip_collect or successful else 1


if __name__ == "__main__":
    sys.exit(main())
