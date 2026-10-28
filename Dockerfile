FROM python:3.12-slim AS build

ENV POETRY_VERSION=2.1.4 \
    POETRY_VIRTUALENVS_IN_PROJECT=true \
    POETRY_NO_INTERACTION=1 \
    PIP_NO_CACHE_DIR=1
RUN pip install "poetry==${POETRY_VERSION}"

WORKDIR /app
COPY pyproject.toml poetry.lock README.md ./
RUN poetry install --only main --no-root
COPY src ./src
RUN poetry install --only main


FROM python:3.12-slim

WORKDIR /app
COPY --from=build /app /app

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    NWT_DATA_DIR=/data

RUN mkdir -p /data && chown 1000:1000 /data
VOLUME /data
EXPOSE 8000
USER 1000:1000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["networth", "serve", "--host", "0.0.0.0", "--port", "8000"]
