FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 10001 seulseul \
    && useradd --uid 10001 --gid seulseul --no-create-home seulseul

COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

COPY alembic.ini ./
COPY migrations ./migrations

USER seulseul

CMD ["python", "-m", "seulseul.main"]
