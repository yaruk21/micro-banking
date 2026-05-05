class TransactionError(Exception):
    pass


class TransactionPermissionError(TransactionError):
    pass


class TransactionValidationError(TransactionError):
    pass


class IdempotencyConflictError(TransactionError):
    pass
