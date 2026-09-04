from datetime import datetime, timedelta

from .errors import DomainValidationError


def ensure_aware(value: datetime, *, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{field} must be timezone-aware")
    return value


def ensure_positive_duration(value: timedelta, *, field: str) -> timedelta:
    if value <= timedelta(0):
        raise DomainValidationError(f"{field} must be greater than zero")
    return value
