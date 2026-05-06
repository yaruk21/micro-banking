import os
import sys
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv
from core.db_config import apply_database_runtime_settings

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_to_bool(name: str, default: bool = False) -> bool:
    """Handle env to bool."""
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_to_int(name: str, default: int) -> int:
    """Handle env to int."""
    return int(os.getenv(name, str(default)))


def env_to_decimal(name: str, default: str) -> Decimal:
    """Handle env to decimal."""
    raw_value = os.getenv(name, default)
    try:
        return Decimal(str(raw_value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(
            f"Environment variable {name} must be a valid decimal value."
        ) from exc


# Core Django environment
SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "unsafe-dev-secret-key-change-me-to-a-long-random-value",
)
DEBUG = env_to_bool("DEBUG", False)
ALLOWED_HOSTS = [
    host.strip()
    for host in os.getenv(
        "ALLOWED_HOSTS",
        "localhost,127.0.0.1,0.0.0.0",
    ).split(",")
    if host.strip()
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "django_filters",
    "drf_spectacular",
    "apps.accounts",
    "apps.transactions",
    "apps.exchange",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "core.structured_logging.RequestIdMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "core.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "core.wsgi.application"
ASGI_APPLICATION = "core.asgi.application"

# Test/runtime database selection
RUNNING_PYTEST = any("pytest" in arg for arg in sys.argv)
RUNNING_TESTS = RUNNING_PYTEST or (
    len(sys.argv) > 1 and sys.argv[1] == "test"
)
USE_POSTGRES_FOR_TESTS = env_to_bool("USE_POSTGRES_FOR_TESTS", False)

if env_to_bool("USE_SQLITE", False) or (
    RUNNING_TESTS and not USE_POSTGRES_FOR_TESTS
):
    # SQLite keeps local development and the default test lane lightweight.
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }
else:
    # Primary handles all writes and consistency-sensitive reads.
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_DB", "micro_banking"),
            "USER": os.getenv("POSTGRES_USER", "micro_banking"),
            "PASSWORD": os.getenv("POSTGRES_PASSWORD", "micro_banking"),
            "HOST": os.getenv("POSTGRES_HOST", "127.0.0.1"),
            "PORT": os.getenv("POSTGRES_PORT", "5432"),
        }
    }
    replica_host = os.getenv("POSTGRES_REPLICA_HOST", "").strip()
    if replica_host:
        # Replica is attached only when explicitly configured in env.
        DATABASES["replica"] = {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.getenv("POSTGRES_REPLICA_DB", os.getenv("POSTGRES_DB", "micro_banking")),
            "USER": os.getenv("POSTGRES_REPLICA_USER", os.getenv("POSTGRES_USER", "micro_banking")),
            "PASSWORD": os.getenv(
                "POSTGRES_REPLICA_PASSWORD",
                os.getenv("POSTGRES_PASSWORD", "micro_banking"),
            ),
            "HOST": replica_host,
            "PORT": os.getenv("POSTGRES_REPLICA_PORT", os.getenv("POSTGRES_PORT", "5432")),
        }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Locale/timezone
LANGUAGE_CODE = "en-us"
TIME_ZONE = {
    "Europe/Kiev": "Europe/Kyiv",
}.get(os.getenv("TIME_ZONE", "UTC"), os.getenv("TIME_ZONE", "UTC"))
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Trusted origins and cache backend
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]
if RUNNING_TESTS:
    # Keep tests deterministic without external Redis dependency.
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "micro-banking-test-cache",
        }
    }
else:
    # Production and dev cache share Redis-style configuration.
    redis_cache_url = os.getenv(
        "REDIS_CACHE_URL",
        os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/1"),
    )
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": redis_cache_url,
        }
    }

EXCHANGE_RATE_CACHE_TIMEOUT_SECONDS = int(
    os.getenv("EXCHANGE_RATE_CACHE_TIMEOUT_SECONDS", "300")
)

# Celery and Redis wiring
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.getenv(
    "CELERY_RESULT_BACKEND",
    CELERY_BROKER_URL,
)
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_ALWAYS_EAGER = RUNNING_TESTS or env_to_bool(
    "CELERY_TASK_ALWAYS_EAGER",
    False,
)
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_TRACK_STARTED = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
REALTIME_REDIS_URL = os.getenv(
    "REALTIME_REDIS_URL",
    os.getenv(
        "REDIS_CACHE_URL",
        os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/1"),
    ),
)
# Read routing stays disabled unless both the flag and replica config are present.
READ_REPLICA_ENABLED = env_to_bool("READ_REPLICA_ENABLED", False) and "replica" in DATABASES

# Background job cadence and FX settings
TRANSACTION_OUTBOX_PUBLISH_INTERVAL_SECONDS = int(
    os.getenv("TRANSACTION_OUTBOX_PUBLISH_INTERVAL_SECONDS", "15")
)
TRANSACTION_OUTBOX_PUBLISH_BATCH_SIZE = int(
    os.getenv("TRANSACTION_OUTBOX_PUBLISH_BATCH_SIZE", "100")
)
TRANSACTION_RECOVERY_INTERVAL_SECONDS = int(
    os.getenv("TRANSACTION_RECOVERY_INTERVAL_SECONDS", "60")
)
SWIFT_TRANSFER_PICKUP_INTERVAL_SECONDS = int(
    os.getenv("SWIFT_TRANSFER_PICKUP_INTERVAL_SECONDS", "60")
)
SWIFT_TRANSFER_PICKUP_BATCH_SIZE = int(
    os.getenv("SWIFT_TRANSFER_PICKUP_BATCH_SIZE", "100")
)
TRANSACTION_PARTITION_MAINTENANCE_INTERVAL_SECONDS = int(
    os.getenv("TRANSACTION_PARTITION_MAINTENANCE_INTERVAL_SECONDS", "86400")
)
TRANSACTION_PARTITION_MONTHS_AHEAD = int(
    os.getenv("TRANSACTION_PARTITION_MONTHS_AHEAD", "3")
)
EXCHANGE_RATE_SYNC_INTERVAL_SECONDS = int(
    os.getenv("EXCHANGE_RATE_SYNC_INTERVAL_SECONDS", "900")
)
FX_EXCHANGE_FEE_RATE = os.getenv("FX_EXCHANGE_FEE_RATE", "0.01")
TRANSACTION_SINGLE_LIMIT_AMOUNT = env_to_decimal(
    "TRANSACTION_SINGLE_LIMIT_AMOUNT",
    "0.00",
)
TRANSACTION_DAILY_LIMIT_AMOUNT = env_to_decimal(
    "TRANSACTION_DAILY_LIMIT_AMOUNT",
    "0.00",
)
TRANSACTION_MONTHLY_LIMIT_AMOUNT = env_to_decimal(
    "TRANSACTION_MONTHLY_LIMIT_AMOUNT",
    "0.00",
)
FRAUD_FREQUENCY_WINDOW_SECONDS = env_to_int(
    "FRAUD_FREQUENCY_WINDOW_SECONDS",
    60,
)
FRAUD_FREQUENCY_MAX_ATTEMPTS = env_to_int(
    "FRAUD_FREQUENCY_MAX_ATTEMPTS",
    10,
)
FRAUD_FREQUENCY_ACTION = os.getenv("FRAUD_FREQUENCY_ACTION", "flag").strip().lower()
FRAUD_AMOUNT_BASELINE_MIN_TRANSACTIONS = env_to_int(
    "FRAUD_AMOUNT_BASELINE_MIN_TRANSACTIONS",
    5,
)
FRAUD_AMOUNT_ANOMALY_MULTIPLIER = env_to_decimal(
    "FRAUD_AMOUNT_ANOMALY_MULTIPLIER",
    "3.00",
)
FRAUD_AMOUNT_ACTION = os.getenv("FRAUD_AMOUNT_ACTION", "flag").strip().lower()
TRANSACTION_2FA_CHALLENGE_FLAGGED = env_to_bool(
    "TRANSACTION_2FA_CHALLENGE_FLAGGED",
    True,
)
TRANSACTION_2FA_CHALLENGE_AMOUNT = env_to_decimal(
    "TRANSACTION_2FA_CHALLENGE_AMOUNT",
    "0.00",
)
TRANSACTION_2FA_CHALLENGE_TTL_SECONDS = env_to_int(
    "TRANSACTION_2FA_CHALLENGE_TTL_SECONDS",
    300,
)
TRANSACTION_2FA_CHALLENGE_MAX_ATTEMPTS = env_to_int(
    "TRANSACTION_2FA_CHALLENGE_MAX_ATTEMPTS",
    3,
)
TRANSACTION_2FA_CHALLENGE_CODE_LENGTH = env_to_int(
    "TRANSACTION_2FA_CHALLENGE_CODE_LENGTH",
    6,
)
TRANSACTION_2FA_EXPOSE_CHALLENGE_CODE = env_to_bool(
    "TRANSACTION_2FA_EXPOSE_CHALLENGE_CODE",
    DEBUG,
)
FRAUD_GEO_COUNTRY_CHANGE_WINDOW_SECONDS = env_to_int(
    "FRAUD_GEO_COUNTRY_CHANGE_WINDOW_SECONDS",
    7200,
)
FRAUD_GEO_IMPOSSIBLE_TRAVEL_SPEED_KMH = env_to_int(
    "FRAUD_GEO_IMPOSSIBLE_TRAVEL_SPEED_KMH",
    900,
)
FRAUD_GEO_ACTION = os.getenv("FRAUD_GEO_ACTION", "flag").strip().lower()
CELERY_BEAT_SCHEDULE = {
    "publish-pending-transaction-outbox": {
        "task": "apps.transactions.workers.celery_tasks.publish_pending_transaction_outbox_task",
        "schedule": TRANSACTION_OUTBOX_PUBLISH_INTERVAL_SECONDS,
        "kwargs": {
            "limit": TRANSACTION_OUTBOX_PUBLISH_BATCH_SIZE,
        },
    },
    "recover-stuck-transfers": {
        "task": "apps.transactions.workers.celery_tasks.recover_stuck_transfers_task",
        "schedule": TRANSACTION_RECOVERY_INTERVAL_SECONDS,
    },
    "dispatch-due-swift-transfers": {
        "task": "apps.transactions.workers.celery_tasks.dispatch_due_swift_transfers_task",
        "schedule": SWIFT_TRANSFER_PICKUP_INTERVAL_SECONDS,
        "kwargs": {
            "limit": SWIFT_TRANSFER_PICKUP_BATCH_SIZE,
        },
    },
    "ensure-transaction-partitions": {
        "task": "apps.transactions.workers.celery_tasks.ensure_transaction_partitions_task",
        "schedule": TRANSACTION_PARTITION_MAINTENANCE_INTERVAL_SECONDS,
    },
    "sync-privatbank-exchange-rates": {
        "task": "apps.exchange.tasks.sync_privatbank_exchange_rates_task",
        "schedule": EXCHANGE_RATE_SYNC_INTERVAL_SECONDS,
    },
}

# Security and connection tuning
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = env_to_bool("SESSION_COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_to_bool("CSRF_COOKIE_SECURE", not DEBUG)
SECURE_SSL_REDIRECT = env_to_bool("SECURE_SSL_REDIRECT", False)
DB_USE_PGBOUNCER = env_to_bool("DB_USE_PGBOUNCER", False)
CONN_MAX_AGE = env_to_int("CONN_MAX_AGE", 0 if DB_USE_PGBOUNCER else 60)
DB_CONN_HEALTH_CHECKS = env_to_bool(
    "DB_CONN_HEALTH_CHECKS",
    CONN_MAX_AGE > 0,
)
DB_DISABLE_SERVER_SIDE_CURSORS = env_to_bool(
    "DB_DISABLE_SERVER_SIDE_CURSORS",
    DB_USE_PGBOUNCER,
)
POSTGRES_SSL_MODE = os.getenv("POSTGRES_SSL_MODE", "").strip()
POSTGRES_APPLICATION_NAME = os.getenv("POSTGRES_APPLICATION_NAME", "").strip()
LIST_CACHE_TIMEOUT_SECONDS = int(os.getenv("LIST_CACHE_TIMEOUT_SECONDS", "60"))
ACCOUNT_BALANCE_CACHE_TIMEOUT_SECONDS = int(
    os.getenv("ACCOUNT_BALANCE_CACHE_TIMEOUT_SECONDS", "60")
)
EXCHANGE_RATE_CACHE_TIMEOUT_SECONDS = int(
    os.getenv("EXCHANGE_RATE_CACHE_TIMEOUT_SECONDS", "300")
)
TRANSACTION_STUCK_THRESHOLD_SECONDS = int(
    os.getenv("TRANSACTION_STUCK_THRESHOLD_SECONDS", "300")
)

# Apply connection lifetime after database aliases are assembled.
DATABASES["default"]["CONN_MAX_AGE"] = CONN_MAX_AGE
if DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql":
    DATABASES["default"] = apply_database_runtime_settings(
        DATABASES["default"],
        conn_max_age=CONN_MAX_AGE,
        conn_health_checks=DB_CONN_HEALTH_CHECKS,
        disable_server_side_cursors=DB_DISABLE_SERVER_SIDE_CURSORS,
        ssl_mode=POSTGRES_SSL_MODE,
        application_name=POSTGRES_APPLICATION_NAME,
    )
if "replica" in DATABASES:
    replica_conn_max_age = env_to_int(
        "POSTGRES_REPLICA_CONN_MAX_AGE",
        CONN_MAX_AGE,
    )
    DATABASES["replica"] = apply_database_runtime_settings(
        DATABASES["replica"],
        conn_max_age=replica_conn_max_age,
        conn_health_checks=env_to_bool(
            "POSTGRES_REPLICA_CONN_HEALTH_CHECKS",
            DB_CONN_HEALTH_CHECKS,
        ),
        disable_server_side_cursors=env_to_bool(
            "POSTGRES_REPLICA_DISABLE_SERVER_SIDE_CURSORS",
            DB_DISABLE_SERVER_SIDE_CURSORS,
        ),
        ssl_mode=os.getenv("POSTGRES_REPLICA_SSL_MODE", POSTGRES_SSL_MODE).strip(),
        application_name=os.getenv(
            "POSTGRES_REPLICA_APPLICATION_NAME",
            POSTGRES_APPLICATION_NAME,
        ).strip(),
    )

# DRF and auth configuration
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
    ),
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {
        "anon": "10/minute",
        "user": "50/minute",
        "register": "5/minute",
        "accounts_read": "60/minute",
        "accounts_write": "20/minute",
        "transactions_read": "60/minute",
        "transactions_write": "20/minute",
    },
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Micro-Banking API",
    "DESCRIPTION": "API for user accounts and internal money transfers.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "COMPONENT_SPLIT_REQUEST": True,
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=30),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=1),
    "AUTH_HEADER_TYPES": ("Bearer",),
}

# Structured JSON logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_context": {
            "()": "core.structured_logging.RequestContextFilter",
        }
    },
    "formatters": {
        "json": {
            "()": "core.structured_logging.JsonFormatter",
        }
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json",
            "filters": ["request_context"],
        }
    },
    "loggers": {
        "apps.accounts": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "apps.transactions": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
