"""UI click logging for logged-in users (admin, cashier, customer)."""
import time
from collections import defaultdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.activity import log_event
from app.deps import session_user_id

router = APIRouter(prefix="/api/activity", tags=["activity"])

_hits: dict[int, list] = defaultdict(list)
_MAX_PER_MIN = 180


def _allow(user_id: int) -> bool:
    now = time.time()
    bucket = [t for t in _hits[user_id] if now - t < 60]
    if len(bucket) >= _MAX_PER_MIN:
        _hits[user_id] = bucket
        return False
    bucket.append(now)
    _hits[user_id] = bucket
    return True


@router.post("/click")
async def ui_click(request: Request):
    user_id = session_user_id(request)
    if user_id is None:
        return JSONResponse({"ok": False}, status_code=401)
    if not _allow(user_id):
        return {"ok": True, "skipped": True}
    try:
        data = await request.json()
    except Exception:
        data = {}
    label = " ".join(str((data or {}).get("label") or "").split())[:200]
    page = str((data or {}).get("page") or "")[:120]
    if not label:
        return {"ok": True, "skipped": True}
    lower = label.lower()
    if any(w in lower for w in ("password", "current password", "new password")):
        label = "Password field"
    role = str(request.session.get("user_role") or "user")
    details = f"{role} clicked “{label}”"
    if page:
        details += f" on {page}"
    log_event("UI Click", details[:500], request=request)
    return {"ok": True}
