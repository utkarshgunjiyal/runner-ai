"""Structured (JSON) logging with per-request correlation IDs.

Every log line is emitted as a single JSON object on stdout and automatically
carries the current ``request_id`` (set by the request middleware in
``main.py``). Extra fields can be attached via the standard ``extra=`` kwarg:

    logger.info("chat.answered", extra={"thread_id": tid, "seq": seq})
"""

import json
import logging
import sys
from contextvars import ContextVar

# Populated by the request-context middleware; "-" outside a request scope.
request_id_ctx: ContextVar[str] = ContextVar("request_id", default="-")

# Attributes present on a bare LogRecord — anything outside this set was passed
# by the caller via ``extra=`` and is worth serialising.
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": request_id_ctx.get(),
        }

        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger (idempotent)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Route uvicorn's loggers through the root JSON handler instead of its own
    # plain-text ones. Access logs are demoted to WARNING because the request
    # middleware already emits a structured "request.completed" line per call.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
