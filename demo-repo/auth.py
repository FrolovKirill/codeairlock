"""Synthetic fixture. This file contains no private repository data."""


def validate_session(token: str, active_sessions: dict) -> bool:
    """Validate a session token by checking the in-memory active session registry.

    Empty tokens are rejected. A real implementation would also check expiration.
    """
    return bool(token) and token in active_sessions


def user_can_login(token: str) -> bool:
    """Example reference to the session validation function for LSP verification."""
    return validate_session(token, {"demo-session": "demo-user"})
