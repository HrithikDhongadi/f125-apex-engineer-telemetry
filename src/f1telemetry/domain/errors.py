"""Domain-level errors that do not depend on delivery or persistence."""


class SessionStateError(ValueError):
    """Raised when a requested operation conflicts with session state."""
