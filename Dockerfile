FROM python:3.11.15-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --shell /bin/bash --uid 10001 appuser

# Copy source code and lock file first so pip install "." installs
# the actual package, not just metadata.
COPY . /app/

# Install production dependencies from the committed lock file for
# reproducible builds.  The lock file pins all direct and transitive
# dependencies with exact versions.
RUN pip install --no-cache-dir --constraint constraints.txt "." \
    && pip check

RUN mkdir -p /app/data \
    && chown -R appuser:appuser /app

USER appuser

CMD ["python", "-m", "newsbot.main"]