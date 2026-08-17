from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

# In-memory storage is deliberate: a single gunicorn instance serves this
# API, and losing counters on restart is acceptable for brute-force
# throttling. Swap storage_uri for redis:// if the API is ever scaled out.
limiter = Limiter(key_func=get_remote_address, storage_uri='memory://')
