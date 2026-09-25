# DataCourt image for self-hosting and dedicated workers (production runs the API on Vercel and
# workers on GitHub Actions; see docs/DEPLOYMENT.md). CPU-only; add the DINOv2 backend with
# --build-arg EXTRAS=ml,server,deep.
#   API + embedded worker (default):  docker run … <image>
#   dedicated / GPU worker:           docker run -e DATACOURT_ROLE=worker … <image> datacourt-worker
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ARG EXTRAS="ml,server"
COPY pyproject.toml alembic.ini ./
COPY datacourt ./datacourt
RUN pip install ".${EXTRAS:+[$EXTRAS]}"

RUN useradd --create-home --uid 10001 datacourt && mkdir -p /app/.data && chown -R datacourt /app
USER datacourt

ENV PORT=8000 \
    ENVIRONMENT=production \
    EXECUTION_BACKEND=embedded \
    RUN_EMBEDDED_WORKER=true
EXPOSE 8000

# Migrations are idempotent and run on every start; the worker runs in-process unless disabled.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn datacourt.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*' --workers 1"]
