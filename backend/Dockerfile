# Shared image for both the API and the worker (command differs per service).
# Small, non-root, unbuffered, with a healthcheck and proper signal handling for
# graceful shutdown.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# tini gives PID-1 signal forwarding + zombie reaping so SIGTERM reaches uvicorn
# for a graceful shutdown (lifespan cleanup: MCP transports, mongo/motor clients).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code only (tests/dev files excluded via .dockerignore).
COPY app ./app

# Run as a non-root user.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

# Liveness only (cheap, no dependencies, no paid calls).
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health/live').status==200 else 1)"

ENTRYPOINT ["tini", "--"]
# Default: API server. --proxy-headers trusts X-Forwarded-* from the reverse
# proxy; --timeout-graceful-shutdown bounds in-flight requests on shutdown.
# The worker service overrides this with: command: python -m app.worker
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "20"]
