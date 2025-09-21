"""Custom exceptions for sdrtrunk-rdio-api."""


class RdioAPIException(Exception):
    """Base exception for sdrtrunk-rdio-api."""

    pass


class InvalidAudioFormatError(RdioAPIException):
    """Raised when audio format is invalid."""

    pass


class RateLimitExceededError(RdioAPIException):
    """Raised when rate limit is exceeded."""

    pass


class InvalidAPIKeyError(RdioAPIException):
    """Raised when API key is invalid or unauthorized."""

    pass


class InvalidSystemIDError(RdioAPIException):
    """Raised when system ID is invalid."""

    pass


class FileSizeError(RdioAPIException):
    """Raised when file size exceeds limits."""

    pass


class DatabaseError(RdioAPIException):
    """Raised when database operations fail."""

    pass


class ConfigurationError(RdioAPIException):
    """Raised when configuration is invalid."""

    pass
