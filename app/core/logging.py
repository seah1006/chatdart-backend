import logging
import sys
from pythonjsonlogger.json import JsonFormatter


def setup_logging(log_level: str = "INFO", use_json: bool = True) -> None:
    """
    애플리케이션 전역 로깅을 설정합니다.

    - use_json=True  (production/staging): JSON 형식 출력 — Docker 로그 수집 도구 호환
    - use_json=False (development):        사람이 읽기 좋은 일반 텍스트 형식 출력
    """
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # 기존 핸들러 제거 (중복 방지)
    root_logger.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)

    if use_json:
        formatter = JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
        )
    else:
        formatter = logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    handler.setFormatter(formatter)
    root_logger.addHandler(handler)

    # 외부 라이브러리 로그 레벨 조정 (노이즈 감소)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
