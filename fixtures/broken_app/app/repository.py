"""Thin data-access layer. Column names come straight from the users table."""

from typing import Any

FAKE_ROWS = {
    1: {"id": 1, "name": "Ada", "mail": "ada@example.com"},
    2: {"id": 2, "name": "Grace", "mail": "grace@example.com"},
}


def get_user(user_id: int) -> dict[str, Any] | None:
    # Renamed the column from `email` to `mail` in migration 0007.
    return FAKE_ROWS.get(user_id)
