"""Structured JSON logging with request/job correlation IDs.

Never log image bytes, file contents, secrets, or raw provider responses.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import traceback

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
job_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("job_id", default=None)

_REDACT_KEYS = {"password", "token", "secret", "authorization", "cookie", "api_key", "database_url"}

# Logs of GitHub Actions runs in a public repository are public. In public mode only these
# fields are written (ids, types, counts, timings), exception messages are dropped and
# tracebacks are reduced to code locations: user-facing strings can contain dataset content.
_PUBLIC_FIELDS = {
    "attempt", "budget_seconds", "count", "deleted_objects", "duration_ms", "error_class", "exc_type",
    "failed", "frames", "has_work", "job_type", "jobs", "ok", "processed", "queued", "reason", "rejected",
    "remaining", "retry", "samples", "slot", "stage", "status", "worker_id", "abandoned_uploads",
    "source_archives", "export_archives", "report_files", "environment", "role", "storage_backend",
    "execution_backend", "embedding_backend", "gemini_configured", "run_embedded_worker",
}  # fmt: skip
_public = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if rid := request_id_var.get():
            payload["request_id"] = rid
        if jid := job_id_var.get():
            payload["job_id"] = jid
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                if _public and k not in _PUBLIC_FIELDS:
                    continue
                payload[k] = "[redacted]" if k.lower() in _REDACT_KEYS else v
        if record.exc_info:
            payload["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            if _public:
                tb = traceback.extract_tb(record.exc_info[2])
                payload["frames"] = [f"{f.filename.rsplit('/', 1)[-1]}:{f.lineno} {f.name}" for f in tb][-12:]
            else:
                payload["exc"] = self.formatException(record.exc_info)[-4000:]
        if _public and not record.name.startswith("datacourt"):
            # Third-party messages are free text (they can quote paths or URLs): keep only the
            # logger name, level and exception type.
            payload["msg"] = "[third-party message withheld]"
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO", public: bool = False) -> None:
    global _public
    _public = _public or public
    root = logging.getLogger()
    if any(isinstance(h.formatter, JsonFormatter) for h in root.handlers):
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers = [handler]
    root.setLevel(level)
    for noisy in ("uvicorn.access", "httpx", "httpcore", "botocore", "boto3", "s3transfer", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log(logger: logging.Logger, level: int, msg: str, **fields: object) -> None:
    logger.log(level, msg, extra={"fields": fields})
