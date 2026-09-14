"""Sanitized errors shared by provider configuration and transport adapters."""


class SourceError(Exception):
    """Only operator-readable messages may cross the data-source boundary."""

    def __init__(self, message: str, *, status_code: int = 422, field: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.field = field
