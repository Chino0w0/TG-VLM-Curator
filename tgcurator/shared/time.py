from datetime import datetime, timedelta

from .errors import DomainValidationError


def ensure_aware(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise DomainValidationError(f"{field} must be a timezone-aware datetime")
    return value


def ensure_positive_duration(value: timedelta, *, field: str) -> timedelta:
    if not isinstance(value, timedelta) or value <= timedelta(0):
        raise DomainValidationError(f"{field} must be a positive duration")
    return value
