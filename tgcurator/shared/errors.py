class DomainValidationError(ValueError):
    """Raised when an aggregate or policy would violate a domain invariant."""
