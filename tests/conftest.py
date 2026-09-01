"""Shared test fixtures."""

import pytest

from src.infrastructure.cache.cache_service import CacheService


@pytest.fixture(autouse=True)
def reset_singletons():
    """Reset process-wide singletons before and after every test."""
    CacheService.reset_instance()
    yield
    CacheService.reset_instance()

