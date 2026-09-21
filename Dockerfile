FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 10001 seulseul \
    && useradd --uid 10001 --gid seulseul --no-create-home seulseul

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

# OAuth 설치·state 파일은 named volume으로 유지하며, 비-root 앱 사용자가 쓸 수 있어야 한다.
RUN mkdir -p /var/lib/seulseul/oauth \
    && chown -R seulseul:seulseul /var/lib/seulseul

COPY alembic.ini ./
COPY migrations ./migrations

USER seulseul

CMD ["python", "-m", "seulseul.main"]
