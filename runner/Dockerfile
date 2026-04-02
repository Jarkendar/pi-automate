FROM n8nio/runners:2.13.2 AS base

# --- Builder stage: use Alpine Python to match runner's musl libc ---
FROM python:3.13-alpine AS builder

RUN apk add --no-cache build-base gfortran openblas-dev && \
    pip install --no-cache-dir --target=/packages \
    pandas \
    matplotlib \
    numpy \
    openpyxl \
    requests

# --- Final stage: copy packages into the runner's venv ---
FROM base

USER root

# Copy installed packages into the runner's venv site-packages
COPY --from=builder /packages /opt/runners/task-runner-python/.venv/lib/python3.13/site-packages/

# Copy OpenBLAS shared libs needed by numpy
COPY --from=builder /usr/lib/libopenblas* /usr/lib/
COPY --from=builder /usr/lib/libgfortran* /usr/lib/
COPY --from=builder /usr/lib/libquadmath* /usr/lib/
COPY --from=builder /usr/lib/libgcc_s* /usr/lib/

# Override the task-runners config to allow stdlib and external imports
COPY n8n-task-runners.json /etc/n8n-task-runners.json

USER runner