from django.test import SimpleTestCase

from core.db_config import apply_database_runtime_settings


class ApplyDatabaseRuntimeSettingsTests(SimpleTestCase):
    """Verify pooled-connection runtime settings for PostgreSQL configs."""

    def test_applies_pooling_friendly_postgres_flags(self) -> None:
        config = apply_database_runtime_settings(
            {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "micro_banking",
            },
            conn_max_age=0,
            conn_health_checks=False,
            disable_server_side_cursors=True,
            ssl_mode="require",
            application_name="micro-banking-api",
        )

        self.assertEqual(config["CONN_MAX_AGE"], 0)
        self.assertFalse(config["CONN_HEALTH_CHECKS"])
        self.assertTrue(config["DISABLE_SERVER_SIDE_CURSORS"])
        self.assertEqual(
            config["OPTIONS"],
            {
                "sslmode": "require",
                "application_name": "micro-banking-api",
            },
        )

    def test_preserves_existing_options_and_omits_empty_optional_values(self) -> None:
        config = apply_database_runtime_settings(
            {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "micro_banking",
                "OPTIONS": {
                    "connect_timeout": 5,
                },
            },
            conn_max_age=60,
            conn_health_checks=True,
        )

        self.assertEqual(config["CONN_MAX_AGE"], 60)
        self.assertTrue(config["CONN_HEALTH_CHECKS"])
        self.assertNotIn("DISABLE_SERVER_SIDE_CURSORS", config)
        self.assertEqual(
            config["OPTIONS"],
            {
                "connect_timeout": 5,
            },
        )
