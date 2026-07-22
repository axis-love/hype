FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --shell /bin/bash --uid 10001 appuser

COPY requirements.txt pyproject.toml /app/

RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . /app

RUN mkdir -p /state/data /state/telethon \
    && chown -R appuser:appuser /app /state

USER appuser

CMD ["python", "bot.py"]
