class TransactionError(Exception):
    """Represent transaction error."""
    pass


class TransactionPermissionError(TransactionError):
    """Represent transaction permission error."""
    pass


class TransactionValidationError(TransactionError):
    """Represent transaction validation error."""
    pass


class TransactionLimitExceededError(TransactionValidationError):
    """Represent transaction limit exceeded validation error."""
    pass


class TransactionFraudBlockedError(TransactionValidationError):
    """Represent a blocked transaction attempt based on fraud checks."""
    pass


class IdempotencyConflictError(TransactionError):
    """Represent idempotency conflict error."""
    pass
