"""Errors whose messages are safe to return to a client (no server paths)."""


class GeoError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"[{code}] {message}")


class ConfigurationError(ValueError):
    """Startup configuration failure, written to stderr only."""
