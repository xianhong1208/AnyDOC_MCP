"""Domain layer: business exceptions and core logic."""

from .exceptions import DomainException, get_http_status_for_exception

__all__ = [
    "DomainException",
    "get_http_status_for_exception",
]
