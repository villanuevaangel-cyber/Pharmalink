from fastapi import Request
from fastapi.responses import JSONResponse


STAFF_ROLES = {"cashier/pharmacist", "admin"}


def session_user_id(request: Request):
    """Return the logged-in id, including 0. None means not logged in."""
    if "user_id" not in request.session:
        return None
    val = request.session.get("user_id")
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


def require_staff(request: Request):
    user_id = session_user_id(request)
    role = str(request.session.get("user_role") or "").lower()
    if user_id is None or role not in STAFF_ROLES:
        return None
    return user_id


def require_admin(request: Request):
    user_id = session_user_id(request)
    role = str(request.session.get("user_role") or "").lower()
    if user_id is None or role != "admin":
        return None
    return user_id
