# DataPro 后端镜像：API 与 worker 共用（worker 只改 command）
# 构建：docker build -t datapro-api:0.1.0 .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
COPY prompts ./prompts
COPY worker ./worker
COPY eval ./eval
COPY importer ./importer
COPY pyproject.toml ./

RUN mkdir -p /app/data/uploads /app/data/exports \
    && useradd --create-home --uid 1000 datapro \
    && chown -R datapro:datapro /app
USER datapro

EXPOSE 8100
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8100/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
