# ── 빌드 스테이지 ─────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /app

# 시스템 의존성 (dart-fss의 lxml 빌드에 필요)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libxml2-dev \
    libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt


# ── 런타임 스테이지 ───────────────────────────────────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# 런타임 시스템 의존성
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 \
    libxslt1.1 \
    libgomp1 \
    fonts-nanum \
    && rm -rf /var/lib/apt/lists/*

# 빌드 스테이지에서 설치된 패키지만 복사
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 애플리케이션 코드 복사
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .

# DB 볼륨 마운트 포인트
VOLUME ["/app/data"]

EXPOSE 8000

# 헬스 체크 — /health 엔드포인트로 컨테이너 상태 확인
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
