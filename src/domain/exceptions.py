"""Domain-level exceptions

A single exception hierarchy. Middleware converts these into HTTP responses.
To add a new exception:
1. Subclass DomainException
2. Add its mapping to get_http_status_for_exception()
"""

from typing import Any


class DomainException(Exception):
    """Base class for all domain exceptions"""

    def __init__(self, message: str, error_code: str, details: dict[str, Any] | None = None):
        self.message = message
        self.error_code = error_code
        self.details = details or {}
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        result = {"error": self.error_code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


# ============================================================================
# Resource Not Found (HTTP 404)
# ============================================================================

class ResourceNotFoundError(DomainException):
    def __init__(self, resource_type: str, identifier: Any, message: str = None):
        if message is None:
            message = f"{resource_type} not found: {identifier}"
        super().__init__(
            message=message,
            error_code=f"{resource_type.upper()}_NOT_FOUND",
            details={"resource_type": resource_type, "identifier": str(identifier)}
        )


# ============================================================================
# Authorization (HTTP 401 / 403)
# ============================================================================

class UnauthorizedAccessError(DomainException):
    def __init__(self, resource_type: str, resource_id: Any, reason: str = None):
        message = f"Not authorized to access {resource_type}: {resource_id}"
        if reason:
            message += f" - {reason}"
        super().__init__(
            message=message,
            error_code="UNAUTHORIZED_ACCESS",
            details={"resource_type": resource_type, "resource_id": str(resource_id)}
        )


class InvalidTokenError(DomainException):
    def __init__(self, reason: str = "Invalid or expired token"):
        super().__init__(message=reason, error_code="INVALID_TOKEN")


# ============================================================================
# Validation (HTTP 400)
# ============================================================================

class ValidationError(DomainException):
    def __init__(self, field: str, message: str, value: Any = None):
        details = {"field": field}
        if value is not None:
            details["invalid_value"] = str(value)
        super().__init__(
            message=f"Validation failed for {field}: {message}",
            error_code="VALIDATION_ERROR",
            details=details
        )


# ============================================================================
# Conflict (HTTP 409)
# ============================================================================

class ConflictError(DomainException):
    def __init__(self, message: str, resource: str = None):
        details = {}
        if resource:
            details["resource"] = resource
        super().__init__(message=message, error_code="CONFLICT", details=details)


# ============================================================================
# Utility
# ============================================================================

def get_http_status_for_exception(exception: DomainException) -> int:
    """Map domain exception to HTTP status code"""
    status_mapping = {
        "VALIDATION_ERROR": 400,
        "INVALID_TOKEN": 401,
        "UNAUTHORIZED_ACCESS": 403,
        "CONFLICT": 409,
    }
    # ResourceNotFoundError has a dynamic error_code (e.g. "FOLDER_NOT_FOUND")
    if isinstance(exception, ResourceNotFoundError):
        return 404
    return status_mapping.get(exception.error_code, 500)
