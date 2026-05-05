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
    return _correlation_id.get()


def set_correlation_id(value: Optional[str]) -> Token:
    return _correlation_id.set(value)


def reset_correlation_id(token: Token) -> None:
    _correlation_id.reset(token)


def get_task_id() -> Optional[str]:
    return _task_id.get()


def set_task_id(value: Optional[str]) -> Token:
    return _task_id.set(value)


def reset_task_id(token: Token) -> None:
    _task_id.reset(token)
