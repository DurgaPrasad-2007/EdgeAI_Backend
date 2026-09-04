from __future__ import annotations

import multiprocessing
import os

# Server socket
# Render assigns the listening port through PORT. BIND remains available for
# local/container overrides, while Docker continues to default to port 8000.
bind = os.getenv("BIND", f"0.0.0.0:{os.getenv('PORT', '8000')}")
backlog = 2048

# Worker processes: standard (2 * CPU) + 1, capped between 2 and 8 by default
workers_default = max(2, min(multiprocessing.cpu_count() * 2 + 1, 8))
workers = int(os.getenv("WEB_CONCURRENCY", str(workers_default)))
worker_class = "uvicorn.workers.UvicornWorker"
worker_connections = 1000
timeout = 30
keepalive = 5

# Process naming
proc_name = "edgefleet-api"

# Logging
loglevel = os.getenv("LOG_LEVEL", "info")
accesslog = "-"
errorlog = "-"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(L)ss'

# Graceful restart & reload
graceful_timeout = 30
max_requests = 2000
max_requests_jitter = 400
