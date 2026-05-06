from __future__ import annotations

from typing import Any


def apply_database_runtime_settings(
    database_config: dict[str, Any],
    *,
    conn_max_age: int,
    conn_health_checks: bool,
    disable_server_side_cursors: bool = False,
    ssl_mode: str = "",
    application_name: str = "",
) -> dict[str, Any]:
    """Return a DB config with runtime connection tuning applied."""
    config = dict(database_config)
    config["CONN_MAX_AGE"] = conn_max_age
    config["CONN_HEALTH_CHECKS"] = conn_health_checks

    if disable_server_side_cursors:
        config["DISABLE_SERVER_SIDE_CURSORS"] = True

    options = dict(config.get("OPTIONS", {}))
    if ssl_mode:
        options["sslmode"] = ssl_mode
    if application_name:
        options["application_name"] = application_name
    if options:
        config["OPTIONS"] = options

    return config
