FROM python:3.11.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --shell /bin/bash --uid 10001 appuser

# Install production dependencies only (no dev extra).
COPY pyproject.toml /app/
RUN pip install --no-cache-dir "." \
    && pip freeze --all > /app/constraints.txt

COPY . /app

RUN mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "-m", "newsbot.main"]