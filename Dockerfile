# syntax=docker/dockerfile:1

# Build the dashboard.
# Built on the build machine's platform: the output is the same for every image.
FROM --platform=$BUILDPLATFORM node:22-slim AS dashboard
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# The gateway serves the API and the built dashboard from one port.
FROM python:3.13-slim
RUN pip install --no-cache-dir uv==0.11.32
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Name and place detection (Presidio with spaCy models for Chinese and
# English). Build with --build-arg PII_NER=none for a smaller image without it.
ARG PII_NER=presidio
ARG SPACY_MODELS="https://github.com/explosion/spacy-models/releases/download/zh_core_web_sm-3.8.0/zh_core_web_sm-3.8.0-py3-none-any.whl https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project $([ "$PII_NER" = presidio ] && echo --extra ner)
COPY src ./src
RUN uv sync --frozen --no-dev $([ "$PII_NER" = presidio ] && echo --extra ner) \
    && if [ "$PII_NER" = presidio ]; then uv pip install --python .venv/bin/python $SPACY_MODELS; fi
COPY --from=dashboard /frontend/dist ./frontend/dist

RUN useradd --system --uid 1000 --home-dir /app ugen \
    && mkdir -p /app/data && chown ugen /app/data
USER ugen

ENV PATH=/app/.venv/bin:$PATH \
    PII_NER=${PII_NER} \
    PII_NER_LANGUAGE=zh,en \
    PII_NER_SPACY_MODEL=zh_core_web_sm,en_core_web_sm \
    HOST=0.0.0.0 \
    PORT=7632 \
    USAGE_DB_PATH=/app/data/ugen.db \
    STATIC_DIR=/app/frontend/dist
VOLUME /app/data
EXPOSE 7632
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=4)"
CMD ["sh", "-c", "exec uvicorn ugen_ai_gateway.main:app --host \"$HOST\" --port \"$PORT\""]
