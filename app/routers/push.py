from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.deps import session_user_id
from app.push import delete_subscription, public_key, save_subscription

router = APIRouter(prefix="/api/push", tags=["push"])


def _audience(request: Request):
    user_id = session_user_id(request)
    role = str(request.session.get("user_role") or "").lower()
    if user_id is None:
        return None
    if role == "customer":
        return {"audience": "customer", "user_id": user_id}
    if role in {"admin", "cashier/pharmacist"}:
        return {"audience": "staff", "user_id": user_id}
    return None


@router.get("/vapid")
def vapid_public(request: Request):
    who = _audience(request)
    if not who:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)
    try:
        key = public_key()
    except Exception as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=503)
    return {"success": True, "publicKey": key}


@router.post("/subscribe")
async def subscribe(request: Request):
    who = _audience(request)
    if not who:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)
    payload = await request.json()
    endpoint = str(payload.get("endpoint") or "").strip()
    keys = payload.get("keys") or {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth_key = str(keys.get("auth") or "").strip()
    if not endpoint or not p256dh or not auth_key:
        return JSONResponse({"success": False, "message": "Missing push subscription."}, status_code=400)
    save_subscription(
        who["audience"],
        endpoint,
        p256dh,
        auth_key,
        user_id=who["user_id"],
    )
    return {"success": True}


@router.post("/unsubscribe")
async def unsubscribe(request: Request):
    if _audience(request) is None:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)
    payload = await request.json()
    delete_subscription(str(payload.get("endpoint") or "").strip())
    return {"success": True}
