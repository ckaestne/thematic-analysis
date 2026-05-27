# Multi-stage build for the multi-DB ta-web dispatcher.
# Build context is the repository root (see docker-compose.yml).

FROM node:20-alpine AS frontend
WORKDIR /repo
COPY frontend/package.json frontend/package-lock.json* ./frontend/
RUN cd frontend && npm ci
COPY frontend ./frontend
COPY src ./src
RUN cd frontend && npm run build


FROM python:3.12-slim AS runtime
ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY --from=frontend /repo/src/thematic_analysis_inc/web_static ./src/thematic_analysis_inc/web_static

RUN uv sync --frozen --no-dev

EXPOSE 8765
ENTRYPOINT ["uv", "run", "--no-dev", "ta-web-multi", \
            "--data-dir", "/data", \
            "--host", "0.0.0.0", \
            "--port", "8765"]
