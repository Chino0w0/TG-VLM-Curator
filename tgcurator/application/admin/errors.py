from __future__ import annotations


class AdminError(Exception):
    """Base application error for administrative operations."""

    status_code: int = 400
    error_code: str = "admin_error"

    def __init__(self, message: str, *, error_code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if error_code is not None:
            self.error_code = error_code


class AdminAuthenticationError(AdminError):
    status_code = 401
    error_code = "unauthenticated"


class AdminCsrfError(AdminError):
    status_code = 403
    error_code = "csrf_invalid"


class AdminNotFoundError(AdminError):
    status_code = 404
    error_code = "not_found"


class AdminConflictError(AdminError):
    status_code = 409
    error_code = "conflict"


class AdminValidationError(AdminError):
    status_code = 422
    error_code = "validation_error"
