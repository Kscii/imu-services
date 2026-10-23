# Linux x86_64 CPU delivery. The base image is pinned to the tested registry digest.
FROM python:3.12-slim-bookworm@sha256:2ed6491b93cd49272ee6de2b5a38440c3448360322c089fc23e370722d74179d AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

FROM base AS dependencies
RUN pip install --no-cache-dir uv==0.12.10
ENV UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY vendor/ vendor/
RUN cd vendor && sha256sum -c SHA256SUMS
COPY src/ src/

FROM dependencies AS query-dependencies
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable

FROM dependencies AS worker-dependencies
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-editable --extra worker

FROM base AS execution
RUN useradd --uid 10001 --create-home imu
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" HOME=/tmp XDG_CACHE_HOME=/tmp/cache \
    NUMBA_CACHE_DIR=/tmp/numba MPLCONFIGDIR=/tmp/matplotlib \
    OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 MKL_NUM_THREADS=2 \
    IMU_WORKER_HEALTH_FILE=/tmp/imu-worker-health
USER 10001:10001
ENTRYPOINT ["imu-service"]

FROM execution AS query
COPY --from=query-dependencies /app/.venv /app/.venv
CMD ["query"]

FROM execution AS runtime
COPY --from=worker-dependencies /app/.venv /app/.venv
CMD ["api"]
