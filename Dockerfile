# syntax=docker/dockerfile:1.7

# ---------- build stage: install dependencies into a virtualenv ----------
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------- runtime stage: slim image, non-root user ----------
FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.title="pdf-ai-assistant" \
      org.opencontainers.image.description="Chat with PDFs using Claude (FastAPI)" \
      org.opencontainers.image.source="https://github.com/momchilborisov05-eng/pdf-ai-chatbot"

# Libraries the mssql-python driver needs on Debian.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libltdl7 libkrb5-3 libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PDF_CHAT_DB=sqlite:////data/pdf_chat.db \
    PORT=8000

RUN groupadd --system app && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=app:app pdf_chat.py review.py questionnaire.py autofill.py storage.py ./
COPY --chown=app:app web ./web

USER app
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

# --proxy-headers: the app runs behind the Container Apps ingress (HTTPS terminated there).
CMD ["sh", "-c", "exec uvicorn web.app:create_app --factory --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
