import os


# Keep pytest independent from a locally reachable PostgreSQL instance.
# The application still uses PostgreSQL by default in Docker/runtime.
if os.getenv("USE_POSTGRES_FOR_TESTS", "0") != "1":
    os.environ.setdefault("USE_SQLITE", "1")
