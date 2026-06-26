FROM node:24-slim AS frontend-builder

WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ca-certificates \
    postgresql-client \
    mariadb-client \
    && rm -rf /var/lib/apt/lists/*

ARG TARGETARCH
RUN if [ "$TARGETARCH" = "amd64" ]; then \
        DENO_ARCH="x86_64-unknown-linux-gnu"; \
    elif [ "$TARGETARCH" = "arm64" ]; then \
        DENO_ARCH="aarch64-unknown-linux-gnu"; \
    else \
        DENO_ARCH="x86_64-unknown-linux-gnu"; \
    fi && \
    curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}.zip" -o /tmp/deno.zip && \
    unzip -o /tmp/deno.zip -d /opt/deno && \
    chmod +x /opt/deno/deno && \
    rm /tmp/deno.zip

ENV GITV_DENO_PATH=/opt/deno/deno

WORKDIR /app

COPY pyproject.toml README.md ./
COPY app/ ./app/

COPY --from=frontend-builder /static/ ./static/

RUN pip install --no-cache-dir -e ".[postgres,mysql]"

RUN mkdir -p data/logs data/backups .deno

EXPOSE 8000

ENV GITV_DATABASE_URL=sqlite+aiosqlite:///./data/gitinthevan.db
ENV GITV_HOST=0.0.0.0
ENV GITV_PORT=8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

CMD ["python", "-m", "app.main"]
