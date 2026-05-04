import os


# Keep pytest independent from a locally reachable PostgreSQL instance.
# The application still uses PostgreSQL by default in Docker/runtime.
os.environ.setdefault("USE_SQLITE", "1")
