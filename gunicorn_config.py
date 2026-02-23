# gunicorn_config.py
# Memory-optimized configuration for Render free tier (512MB RAM)

import multiprocessing
import os

# Bind to the port Render provides
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"

# Use only 1 worker to minimize memory usage on free tier
workers = 1

# Worker class - sync is most memory efficient
worker_class = 'sync'

# Timeout settings
timeout = 300  # 5 minutes for OCR processing
graceful_timeout = 30
keepalive = 2

# Memory management
max_requests = 100  # Restart worker after 100 requests to prevent memory leaks
max_requests_jitter = 20  # Add randomness to prevent all workers restarting at once

# Preload app to share memory across workers (not useful with 1 worker, but good practice)
preload_app = False  # Set to False to avoid memory issues on startup

# Logging
accesslog = '-'
errorlog = '-'
loglevel = 'info'

# Process naming
proc_name = 'ipfs-mapper'

# Limit request line and header sizes to save memory
limit_request_line = 4096
limit_request_fields = 100
limit_request_field_size = 8190

