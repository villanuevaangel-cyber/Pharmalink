from pathlib import Path

from fastapi import APIRouter, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from app.activity import write_activity_log
from app.db import fetch_all, fetch_one, get_conn
from app.deps import require_staff
from app.profile_photos import ALLOWED_EXT, photo_response, store_staff_photo

router = APIRouter(prefix="/api/staff", tags=["staff"])


def _ensure_staff_notification_reads() -> None:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS staff_notification_reads (
                    user_id INTEGER NOT NULL,
                    notif_key VARCHAR(80) NOT NULL,
                    read_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, notif_key)
                )
                """
            )


def _fmt_staff_dt(val) -> str:
    if val is None:
        return ""
    if hasattr(val, "strftime"):
        return val.strftime("%b %d, %Y %I:%M %p")
    return str(val)


def _staff_nav(atype: str) -> tuple[str, str]:
    t = (atype or "").lower()
    if t == "auto_po":
        return "deliveries", ""
    if t == "expired":
        return "inventory", "all"
    if t in {"expiring", "expiring_30"}:
        return "inventory", "expiring30"
    if t == "expiring_90":
        return "inventory", "expiring"
    if t in {"low", "low_stock", "out", "out_of_stock"}:
        return "inventory", "low"
    return "dashboard", ""


def _icon_for(atype: str) -> str:
    if atype in {"low", "low_stock"}:
        return "fa-box-open"
    if atype in {"out", "out_of_stock"}:
        return "fa-circle-xmark"
    if "expir" in atype:
        return "fa-triangle-exclamation"
    if atype == "auto_po":
        return "fa-cart-plus"
    if atype == "expired":
        return "fa-ban"
    return "fa-bell"


_NOTE_TITLES = {
    "expired": "Expired lot",
    "expiring": "Near expiry",
    "expiring_30": "Near expiry (30 days)",
    "expiring_90": "Near expiry (90 days)",
    "low": "Low stock",
    "low_stock": "Low stock",
    "out": "Out of stock",
    "out_of_stock": "Out of stock",
    "auto_po": "Automatic purchase order",
}


def _pack_note(notif_key: str, atype: str, message: str, date: str = "", title: str = "") -> dict:
    target, inv_filter = _staff_nav(atype)
    kind = atype or "alert"
    raw = (title or "").strip()
    friendly = _NOTE_TITLES.get(kind, "Alert")
    if not raw or raw.lower() in {kind, kind.replace("_", " "), "out", "low", "alert"}:
        display_title = friendly
    else:
        display_title = raw
    return {
        "notif_key": notif_key,
        "type": kind,
        "icon": _icon_for(kind),
        "title": display_title,
        "message": message,
        "date": date,
        "target": target,
        "inv_filter": inv_filter,
    }


def _unread_staff_notes(user_id: int) -> tuple[int, list[dict]]:
    notes = []
    persisted = fetch_all(
        """
        SELECT a.alert_id, a.alert_type, a.title, a.message, a.created_at
        FROM system_alerts a
        LEFT JOIN staff_notification_reads r
          ON r.user_id = %s AND r.notif_key = ('a:' || a.alert_id::text)
        WHERE a.created_at >= (NOW() - INTERVAL '14 days')
          AND r.notif_key IS NULL
        ORDER BY
            CASE a.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
            a.created_at DESC
        LIMIT 40
        """,
        (user_id,),
    )
    unread_row = fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM system_alerts a
        LEFT JOIN staff_notification_reads r
          ON r.user_id = %s AND r.notif_key = ('a:' || a.alert_id::text)
        WHERE a.created_at >= (NOW() - INTERVAL '14 days')
          AND r.notif_key IS NULL
        """,
        (user_id,),
    )
    unread_count = int((unread_row or {}).get("n") or 0)
    has_recent_alerts = fetch_all(
        """
        SELECT 1 FROM system_alerts
        WHERE created_at >= (NOW() - INTERVAL '14 days')
        LIMIT 1
        """
    )
    if has_recent_alerts:
        for row in persisted:
            atype = str(row["alert_type"] or "alert")
            notes.append(
                _pack_note(
                    f"a:{row['alert_id']}",
                    atype,
                    row["message"],
                    _fmt_staff_dt(row.get("created_at")),
                    row.get("title") or "",
                )
            )
        return unread_count, notes

    read_keys = {
        r["notif_key"]
        for r in fetch_all(
            "SELECT notif_key FROM staff_notification_reads WHERE user_id = %s",
            (user_id,),
        )
    }
    for row in fetch_all(
        """
        SELECT dm.drug_id, dm.generic_name, dm.brand_name, dm.minimum_stock,
               COALESCE(SUM(il.current_stock), 0) AS on_hand
        FROM drugs_master dm
        LEFT JOIN inventory_lots il ON dm.drug_id = il.drug_id AND il.is_active = 1
        WHERE dm.is_active = 1 AND dm.stock_status = 'low'
        GROUP BY dm.drug_id, dm.generic_name, dm.brand_name, dm.minimum_stock
        ORDER BY on_hand ASC
        LIMIT 10
        """
    ):
        key = f"live:low:{row['drug_id']}"
        if key in read_keys:
            continue
        notes.append(
            _pack_note(
                key,
                "low_stock",
                f"Low stock: {row['generic_name']} ({row['brand_name']}) - {row['on_hand']} left (min {row['minimum_stock']}). Reorder soon.",
            )
        )
    for row in fetch_all(
        "SELECT drug_id, generic_name, brand_name FROM drugs_master WHERE is_active = 1 AND stock_status = 'out' LIMIT 5"
    ):
        key = f"live:out:{row['drug_id']}"
        if key in read_keys:
            continue
        notes.append(
            _pack_note(
                key,
                "out_of_stock",
                f"Out of stock: {row['generic_name']} ({row['brand_name']}) - restock immediately.",
            )
        )
    for row in fetch_all(
        """
        SELECT il.lot_inventory_id, il.lot_number, il.expiration_date, d.generic_name, d.brand_name,
               (il.expiration_date - CURRENT_DATE) AS days_left
        FROM inventory_lots il
        JOIN drugs_master d ON il.drug_id = d.drug_id
        WHERE il.is_active = 1
          AND il.current_stock > 0
          AND il.expiration_date > CURRENT_DATE
          AND il.expiration_date <= (CURRENT_DATE + INTERVAL '30 days')
        ORDER BY il.expiration_date ASC
        LIMIT 8
        """
    ):
        key = f"live:exp:{row['lot_inventory_id']}"
        if key in read_keys:
            continue
        notes.append(
            _pack_note(
                key,
                "expiring",
                f"Expiring soon: {row['generic_name']} ({row['brand_name']}), lot {row['lot_number']} - {row['days_left']} day(s) left.",
            )
        )
    return len(notes), notes


@router.get("/notifications")
def notifications(request: Request):
    user_id = require_staff(request)
    if user_id is None:
        return {"count": 0, "notifications": []}
    try:
        _ensure_staff_notification_reads()
    except Exception:
        pass
    count, notes = _unread_staff_notes(user_id)
    return {"count": count, "notifications": notes}


@router.post("/notifications/read")
async def mark_notification_read(request: Request, notif_key: str = Query("")):
    user_id = require_staff(request)
    key = (notif_key or "").strip()
    if not key:
        try:
            body = await request.json()
            if isinstance(body, dict):
                key = str(body.get("notif_key") or "").strip()
        except Exception:
            pass
    key = key[:80]
    if user_id is None or not key:
        return JSONResponse({"success": False, "message": "Invalid request."}, status_code=400)
    try:
        _ensure_staff_notification_reads()
    except Exception:
        return JSONResponse({"success": False, "message": "Could not save read state."}, status_code=500)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO staff_notification_reads (user_id, notif_key)
                VALUES (%s, %s)
                ON CONFLICT (user_id, notif_key) DO NOTHING
                """,
                (user_id, key),
            )
    count, notes = _unread_staff_notes(user_id)
    return {"success": True, "count": count, "notifications": notes}


@router.get("/profile-photo")
def get_profile_photo(request: Request):
    user_id = require_staff(request)
    if not user_id:
        return JSONResponse({"success": False, "message": "Not authorized."}, status_code=401)
    row = fetch_one(
        "SELECT profile_image, profile_image_data, profile_image_mime FROM staff_info WHERE user_id = %s",
        (user_id,),
    )
    if not row:
        return JSONResponse({"success": False, "message": "Not found."}, status_code=404)
    image = photo_response(row.get("profile_image_data"), row.get("profile_image_mime"), row.get("profile_image"))
    if image is None:
        return JSONResponse({"success": False, "message": "No photo."}, status_code=404)
    return image


@router.post("/profile-picture")
async def profile_picture(request: Request, profile_image: UploadFile = File(...)):
    user_id = require_staff(request)
    if not user_id:
        return JSONResponse({"success": False, "message": "Not authorized."}, status_code=401)
    if not profile_image.filename:
        return {"success": False, "message": "Please choose an image to upload."}
    ext = Path(profile_image.filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED_EXT:
        return {"success": False, "message": "Only JPG, PNG, WEBP, or GIF images are allowed."}
    content = await profile_image.read()
    if len(content) > 3 * 1024 * 1024:
        return {"success": False, "message": "Image must be under 3MB."}
    path = store_staff_photo(user_id, content, ext)
    with get_conn() as conn:
        with conn.cursor() as cur:
            write_activity_log(cur, "Update Profile Picture", "Uploaded a new profile picture.", request=request)
    return {"success": True, "path": path}
