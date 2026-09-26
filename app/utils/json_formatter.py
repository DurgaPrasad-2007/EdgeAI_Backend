import json
import logging
from datetime import datetime, timezone

SEVERITY_MAP = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO",
    logging.WARNING: "WARNING",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRITICAL",
}

_STANDARD_LOGRECORD_KEYS = frozenset(
    logging.LogRecord(
        name="",
        level=0,
        pathname="",
        lineno=0,
        msg="",
        args=(),
        exc_info=None,
    ).__dict__.keys()
)


class JsonFormatter(logging.Formatter):
    """Structured JSON formatter matching cloud logging standards."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp_str = datetime.fromtimestamp(record.created, timezone.utc).isoformat()

        log_entry = {
            "timestamp": timestamp_str,
            "severity": SEVERITY_MAP.get(record.levelno, "DEFAULT"),
            "logger": record.name,
            "process": record.process,
            "file": record.pathname,
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        for key, value in record.__dict__.items():
            if key not in _STANDARD_LOGRECORD_KEYS:
                log_entry[key] = value

        return json.dumps(log_entry, default=str)
