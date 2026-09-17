"""Shared activity-log writes. Actor is first + last name when a session is present."""
from fastapi import Request

from app.db import get_conn, next_id


def actor_display_name(request: Request | None = None, fallback: str = "User") -> str:
    if request is None:
        return fallback
    first = str(request.session.get("user_first_name") or "").strip()
    last = str(request.session.get("user_last_name") or "").strip()
    name = f"{first} {last}".strip()
    if name:
        return name
    role = str(request.session.get("user_role") or "").strip()
    return role or fallback


def write_activity_log(cur, action: str, details: str, request: Request | None = None, actor: str | None = None) -> None:
    log_id = next_id(cur, "activity_logs", "log_id")
    name = (actor or actor_display_name(request)).strip() or "User"
    cur.execute(
        "INSERT INTO activity_logs (log_id, admin_name, action, details) VALUES (%s, %s, %s, %s)",
        (log_id, name, action, details),
    )


def log_event(action: str, details: str, request: Request | None = None, actor: str | None = None) -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            write_activity_log(cur, action, details, request=request, actor=actor)
