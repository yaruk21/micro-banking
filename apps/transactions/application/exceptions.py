class TransactionError(Exception):
    """Represent transaction error."""
    pass


class TransactionPermissionError(TransactionError):
    """Represent transaction permission error."""
    pass


class TransactionValidationError(TransactionError):
    """Represent transaction validation error."""
    pass


class IdempotencyConflictError(TransactionError):
    """Represent idempotency conflict error."""
    pass
