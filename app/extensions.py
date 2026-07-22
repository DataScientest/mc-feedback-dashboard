from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# Shared rate limiter. In-memory storage is per-process: with several
# Gunicorn workers each worker has its own counters, which still caps
# brute force at (limit x workers) - acceptable for this deployment.
limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")
