"""Logging configuration and utilities for EdgeFleet application."""

from __future__ import annotations

import logging


class SuppressTransientNetworkFilter(logging.Filter):
    """Filter to suppress noisy keep-alive or transient connection reset messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if "PollerCompletionQueue" in message or "_handle_events" in message:
            return False
        return True


logger = logging.getLogger("edgefleet")
app_logger = logging.getLogger("app")
