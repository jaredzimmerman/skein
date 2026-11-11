"""The one error type skein's service raises.

Transports map ``code`` to their own shapes (REST status codes, MCP error
dicts); the message is written for the assistant reading it.
"""

from __future__ import annotations

CODES = frozenset({"not_found", "forbidden", "invalid", "conflict"})


class SkeinError(Exception):
    """A request skein refuses. ``code`` is one of :data:`CODES`."""

    def __init__(self, code: str, message: str) -> None:
        if code not in CODES:
            raise ValueError(f"unknown SkeinError code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict[str, str]:
        """The JSON shape transports return: ``{"error": message, "code": code}``."""
        return {"error": self.message, "code": self.code}

    def __repr__(self) -> str:
        return f"SkeinError({self.code!r}, {self.message!r})"
