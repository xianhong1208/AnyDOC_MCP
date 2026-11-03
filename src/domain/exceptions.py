"""Domain-level exceptions

A single exception hierarchy. Middleware converts these into HTTP responses.
To add a new exception:
1. Subclass DomainException
2. If it should not be a 500, map its error_code in _HTTP_STATUS_BY_ERROR_CODE
"""

from typing import Any


class DomainException(Exception):
    """Base class for all domain exceptions"""

    def __init__(self, message: str, error_code: str, details: dict[str, Any] | None = None):
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        super().__init__(message)


# error_code -> HTTP status. The current domain exceptions (UnsupportedFormatError,
# NoConversionPathError, ConversionFailedError in convert/types.py) all map to 500.
_HTTP_STATUS_BY_ERROR_CODE: dict[str, int] = {}


def get_http_status_for_exception(exception: DomainException) -> int:
    """Map domain exception to HTTP status code"""
    return _HTTP_STATUS_BY_ERROR_CODE.get(exception.error_code, 500)
