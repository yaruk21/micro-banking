from contextvars import ContextVar, Token
from typing import Optional

_correlation_id: ContextVar[Optional[str]] = ContextVar(
    "correlation_id",
    default=None,
)
_task_id: ContextVar[Optional[str]] = ContextVar(
    "task_id",
    default=None,
)


def get_correlation_id() -> Optional[str]:
    """Return correlation id."""
    return _correlation_id.get()


def set_correlation_id(value: Optional[str]) -> Token:
    """Set correlation id."""
    return _correlation_id.set(value)


def reset_correlation_id(token: Token) -> None:
    """Handle reset correlation id."""
    _correlation_id.reset(token)


def get_task_id() -> Optional[str]:
    """Return task id."""
    return _task_id.get()


def set_task_id(value: Optional[str]) -> Token:
    """Set task id."""
    return _task_id.set(value)


def reset_task_id(token: Token) -> None:
    """Handle reset task id."""
    _task_id.reset(token)
