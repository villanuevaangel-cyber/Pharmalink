import random
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from psycopg2 import IntegrityError
from psycopg2.extras import RealDictCursor

from app.activity import actor_display_name, write_activity_log
from app.automation import normalize_consignment_policy, run_consignment_cross_check
from app.db import fetch_all, fetch_one, get_conn, next_id
from app.deps import require_admin
from app.pricing import reprice_lots_for_category, resolve_selling_price
from app.profile_photos import resolve_photo_url, staff_photo_url
from app.security import hash_password
from app.stock import sync_stock_status_for_drug
from app.validation import (
    CUSTOMER_TYPES,
    password_complexity_error,
    validate_drug_fields,
    validate_profile_fields,
    prepare_profile_fields,
    validate_username,
)

router = APIRouter(prefix="/api/admin", tags=["admin"])
ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_AVATAR = "https://cdn-icons-png.flaticon.com/512/2922/2922510.png"
MANILA = ZoneInfo("Asia/Manila")


def _manila_today():
    return datetime.now(MANILA).date()


def _unauthorized():
    return JSONResponse({"success": False, "message": "Not authorized."}, status_code=401)


def _jsonable(value):
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _row(row):
    return _jsonable(dict(row)) if row else None


def _rows(rows):
    return [_row(r) for r in rows]


async def _body(request: Request):
    ctype = request.headers.get("content-type") or ""
    if "application/json" in ctype:
        return await request.json()
    form = await request.form()
    return {k: v for k, v in form.items()}


def _log(cur, request: Request, action: str, details: str):
    write_activity_log(cur, action, details, request=request)


def _sync_all_stock(cur):
    cur.execute(
        """
        UPDATE drugs_master dm
        SET stock_status = CASE
            WHEN COALESCE((SELECT SUM(current_stock) FROM inventory_lots WHERE is_active = 1 AND expiration_date >= CURRENT_DATE AND drug_id = dm.drug_id), 0) <= 0 THEN 'out'
            WHEN COALESCE((SELECT SUM(current_stock) FROM inventory_lots WHERE is_active = 1 AND expiration_date >= CURRENT_DATE AND drug_id = dm.drug_id), 0) <= dm.minimum_stock THEN 'low'
            ELSE 'ok'
        END
        WHERE dm.is_active = 1
        """
    )


def _parse_day(value: str):
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


@router.get("/dashboard")
def dashboard(
    request: Request,
    period: str = "month",
    start_date: str = Query(""),
    end_date: str = Query(""),
):
    if not require_admin(request):
        return _unauthorized()
    today = date.today()
    start = _parse_day(start_date)
    end = _parse_day(end_date)
    if start and end:
        if start > end:
            start, end = end, start
        if end > today:
            end = today
        if start > today:
            start = today
        period = "custom"
        if start == end == today:
            period_label = "Today"
        else:
            period_label = f"{start.strftime('%b %d, %Y')} - {end.strftime('%b %d, %Y')}"
    elif period == "today":
        start = end = today
        period_label = "Today"
    elif period == "year":
        start, end = date(today.year, 1, 1), min(date(today.year, 12, 31), today)
        period_label = "This Year"
    else:
        period = "month"
        start, end = date(today.year, today.month, 1), today
        period_label = "This Month"
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _sync_all_stock(cur)
            cur.execute(
                "SELECT COUNT(u.user_id) AS n FROM users u JOIN role r ON u.role_id=r.role_id WHERE LOWER(r.role_name) != 'customer'"
            )
            staff_count = int(cur.fetchone()["n"] or 0)
            cur.execute("SELECT COUNT(customer_id) AS n FROM customers")
            customer_count = int(cur.fetchone()["n"] or 0)
            cur.execute(
                "SELECT COALESCE(SUM(total_amount), 0) AS total FROM sales WHERE date_created::date BETWEEN %s AND %s AND status = 'completed'",
                (start.isoformat(), end.isoformat()),
            )
            total_sales = float(cur.fetchone()["total"] or 0)
            cur.execute("SELECT status, COUNT(*) AS cnt FROM suppliers GROUP BY status")
            supplier_count = inactive_supplier_count = 0
            for row in cur.fetchall():
                if row["status"] == "Active":
                    supplier_count = int(row["cnt"])
                else:
                    inactive_supplier_count += int(row["cnt"])
            cur.execute("SELECT stock_status, COUNT(*) AS cnt FROM drugs_master WHERE is_active = 1 GROUP BY stock_status")
            ok_stock = low_stock = out_stock = 0
            for row in cur.fetchall():
                if row["stock_status"] == "low":
                    low_stock = int(row["cnt"])
                elif row["stock_status"] == "out":
                    out_stock = int(row["cnt"])
                else:
                    ok_stock += int(row["cnt"])
            cur.execute(
                """
                SELECT COUNT(*) AS cnt FROM inventory_lots
                WHERE is_active = 1 AND current_stock > 0
                  AND expiration_date >= CURRENT_DATE
                  AND expiration_date <= (CURRENT_DATE + INTERVAL '30 days')
                """
            )
            expiring_30_count = int(cur.fetchone()["cnt"] or 0)
    return {
        "period": period,
        "period_label": period_label,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "staff_count": staff_count,
        "customer_count": customer_count,
        "total_sales": total_sales,
        "supplier_count": supplier_count,
        "inactive_supplier_count": inactive_supplier_count,
        "ok_stock": ok_stock,
        "low_stock": low_stock,
        "out_stock": out_stock,
        "expiring_30_count": expiring_30_count,
        "server_datetime": datetime.now().strftime("%A, %B %d, %Y - %I:%M:%S %p").replace(" 0", " "),
    }


@router.get("/profile")
def get_profile(request: Request):
    user_id = require_admin(request)
    if not user_id:
        return _unauthorized()
    staff = fetch_one(
        """
        SELECT si.first_name, si.middle_name, si.last_name, si.email, si.phone_number, si.address, si.profile_image,
               (si.profile_image_data IS NOT NULL) AS has_profile_photo, u.username
        FROM staff_info si
        JOIN users u ON si.user_id = u.user_id
        WHERE si.user_id = %s
        """,
        (user_id,),
    ) or {}
    staff = dict(staff) if staff else {}
    staff["email"] = staff.get("email") or staff.get("username") or ""
    has_photo = bool(staff.pop("has_profile_photo", False))
    staff["profile_image"] = resolve_photo_url(staff.get("profile_image"), has_photo, staff_photo_url(user_id))
    staff["first_name"] = staff.get("first_name") or request.session.get("user_first_name") or "Admin"
    return {"success": True, "staff": staff}


@router.post("/profile")
async def update_profile(request: Request):
    user_id = require_admin(request)
    if not user_id:
        return _unauthorized()
    data = await request.json()
    err, packed = prepare_profile_fields(
        str(data.get("first_name") or ""),
        str(data.get("last_name") or ""),
        str(data.get("email") or ""),
        str(data.get("phone_number") or ""),
        str(data.get("address") or ""),
        str(data.get("middle_name") or ""),
    )
    if err:
        return {"success": False, "message": err}
    first_name = packed["first_name"]
    middle_name = packed["middle_name"]
    last_name = packed["last_name"]
    email = packed["email"]
    phone_number = packed["phone_number"]
    address = packed["address"]
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            staff_id = next_id(cur, "staff_info", "staff_id")
            cur.execute(
                """
                INSERT INTO staff_info (staff_id, user_id, first_name, middle_name, last_name, email, phone_number, address)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    first_name = EXCLUDED.first_name, middle_name = EXCLUDED.middle_name,
                    last_name = EXCLUDED.last_name, email = EXCLUDED.email,
                    phone_number = EXCLUDED.phone_number, address = EXCLUDED.address
                """,
                (staff_id, user_id, first_name, middle_name, last_name, email, phone_number, address),
            )
            cur.execute(
                "SELECT first_name, middle_name, last_name, email, phone_number, address FROM staff_info WHERE user_id = %s",
                (user_id,),
            )
            saved = cur.fetchone()
            write_activity_log(
                cur,
                "Update Profile",
                f"Updated admin profile ({first_name} {last_name}).",
                actor=f"{first_name} {last_name}".strip(),
            )
    if not saved:
        return {"success": False, "message": "Save did not persist correctly. Please try again or contact support."}
    request.session["user_first_name"] = saved["first_name"]
    request.session["user_last_name"] = saved.get("last_name") or ""
    return {"success": True, "message": "Profile updated successfully.", **dict(saved)}


@router.get("/drugs")
def list_drugs(request: Request, status: str = "active"):
    if not require_admin(request):
        return _unauthorized()
    sql = "SELECT drug_id, generic_name, brand_name, dosage, form, category, minimum_stock, COALESCE(procurement_type, 'purchase') AS procurement_type, is_active, barcode FROM drugs_master "
    if status == "active":
        sql += "WHERE is_active = 1 "
    sql += "ORDER BY generic_name ASC"
    rows = fetch_all(sql)
    out = []
    for row in rows:
        item = _row(row)
        item["minimum_stock"] = int(item.get("minimum_stock") or 0)
        item["is_active"] = int(item.get("is_active") or 0)
        out.append(item)
    return out


@router.post("/drugs")
async def add_drug(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    generic_name = str(data.get("generic_name") or "").strip()
    dosage = str(data.get("dosage") or "").strip()
    form = str(data.get("form") or "").strip()
    brand_name = str(data.get("brand_name") or "").strip()
    category = str(data.get("category") or "").strip()
    barcode_in = str(data.get("barcode") or "").strip()
    drug_err = validate_drug_fields(
        generic_name, dosage, form, category, data.get("minimum_stock"), brand_name, barcode_in
    )
    if drug_err:
        return {"success": False, "message": drug_err}
    procurement = str(data.get("procurement_type") or "").strip().lower()
    if procurement not in {"purchase", "consignment"}:
        return {"success": False, "message": "Choose Purchased or Consignment. This is not set automatically."}
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            drug_id = next_id(cur, "drugs_master", "drug_id")
            barcode = barcode_in or f"PLK{drug_id:06d}"
            try:
                cur.execute(
                    """
                    INSERT INTO drugs_master (drug_id, generic_name, brand_name, dosage, form, category, minimum_stock, procurement_type, barcode)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        drug_id,
                        generic_name,
                        brand_name or None,
                        dosage,
                        form,
                        category,
                        int(data.get("minimum_stock") or 0),
                        procurement,
                        barcode,
                    ),
                )
            except IntegrityError as exc:
                err = str(exc).lower()
                if "barcode" in err:
                    return {"success": False, "message": "That barcode is already used by another drug."}
                return {"success": False, "message": "Error: This exact drug (Generic Name, Dosage, Form) already exists."}
            _log(cur, request, "Add Drug", f"Added new drug: {generic_name} ({dosage}, {form}) barcode {barcode}")
    return {"success": True, "id": drug_id, "barcode": barcode}


@router.post("/drugs/update")
async def update_drug(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    drug_id = int(data.get("drug_id") or 0)
    if drug_id <= 0:
        return {"success": False, "message": "Drug ID is required."}
    generic_name = str(data.get("generic_name") or "").strip()
    dosage = str(data.get("dosage") or "").strip()
    form = str(data.get("form") or "").strip()
    brand_name = str(data.get("brand_name") or "").strip()
    category = str(data.get("category") or "").strip()
    barcode_in = str(data.get("barcode") or "").strip()
    barcode = barcode_in or f"PLK{drug_id:06d}"
    drug_err = validate_drug_fields(
        generic_name, dosage, form, category, data.get("minimum_stock"), brand_name, barcode_in
    )
    if drug_err:
        return {"success": False, "message": drug_err}
    procurement = str(data.get("procurement_type") or "").strip().lower()
    if procurement not in {"purchase", "consignment"}:
        return {"success": False, "message": "Choose Purchased or Consignment. This is not set automatically."}
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            try:
                cur.execute(
                    """
                    UPDATE drugs_master
                    SET generic_name=%s, brand_name=%s, dosage=%s, form=%s, category=%s, minimum_stock=%s, barcode=%s, procurement_type=%s
                    WHERE drug_id=%s
                    """,
                    (
                        generic_name,
                        brand_name or None,
                        dosage,
                        form,
                        category,
                        int(data.get("minimum_stock") or 0),
                        barcode,
                        procurement,
                        drug_id,
                    ),
                )
            except IntegrityError as exc:
                err = str(exc).lower()
                if "barcode" in err:
                    return {"success": False, "message": "That barcode is already used by another drug."}
                return {"success": False, "message": str(exc)}
            sync_stock_status_for_drug(cur, drug_id)
            _log(cur, request, "Edit Drug", f"Edited drug ID {drug_id} to {data.get('generic_name')}")
    return {"success": True}


@router.get("/drugs/deactivate")
def deactivate_drug(request: Request, id: int = 0):
    if not require_admin(request):
        return _unauthorized()
    if id <= 0:
        return JSONResponse({"success": False, "message": "Drug ID is required."}, status_code=400)
    drug = fetch_one("SELECT generic_name, brand_name FROM drugs_master WHERE drug_id = %s", (id,))
    if not drug:
        return {"success": False, "message": "Drug not found."}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE drugs_master SET is_active = 0 WHERE drug_id = %s", (id,))
            label = f"{drug['generic_name']}" + (f" ({drug['brand_name']})" if drug.get("brand_name") else "")
            _log(cur, request, "Deactivate Drug", f"Drug '{label}' (ID: {id}) has been deactivated.")
    return {"success": True, "message": "Drug archived successfully."}


@router.get("/drugs/reactivate")
def reactivate_drug(request: Request, id: int = 0):
    if not require_admin(request):
        return _unauthorized()
    if id <= 0:
        return JSONResponse({"success": False, "message": "Drug ID is required."}, status_code=400)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE drugs_master SET is_active = 1 WHERE drug_id = %s", (id,))
            _log(cur, request, "Reactivate Drug", f"Reactivated drug_master ID: {id}")
    return {"success": True, "message": "Drug definition reactivated successfully."}


@router.get("/lots")
def list_lots(request: Request, status: str = "active", exclude_expired: str = "1"):
    if not require_admin(request):
        return _unauthorized()
    sql = """
        SELECT l.lot_inventory_id, l.lot_number, l.expiration_date, l.current_stock, l.price, l.cost_price,
               l.supplier, l.is_active, l.return_status, s.consignment_policy AS supplier_consignment_policy,
               d.drug_id, d.generic_name, d.brand_name, d.dosage, d.form, d.category,
               d.minimum_stock, d.stock_status
        FROM inventory_lots l
        JOIN drugs_master d ON l.drug_id = d.drug_id
        LEFT JOIN suppliers s ON l.supplier = s.supplier_id
    """
    where = []
    if status == "active":
        where.append("l.is_active = 1")
    elif status == "archived":
        where.append("l.is_active = 0")
    if exclude_expired == "1":
        where.append("l.expiration_date >= CURRENT_DATE")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY l.expiration_date ASC"
    out = []
    for row in fetch_all(sql):
        item = _row(row)
        item["current_stock"] = int(item.get("current_stock") or 0)
        item["minimum_stock"] = int(item.get("minimum_stock") or 0)
        item["price"] = float(item.get("price") or 0)
        item["is_active"] = int(item.get("is_active") or 0)
        out.append(item)
    return out


@router.post("/lots")
async def add_lot(request: Request):
    if not require_admin(request):
        return _unauthorized()
    return {
        "success": False,
        "message": "New stock lots are added by receiving a delivery. Open Deliveries and receive the purchase order.",
    }


@router.post("/lots/update")
async def update_lot(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    lot_id = int(data.get("lot_inventory_id") or 0)
    if lot_id <= 0:
        return JSONResponse({"success": False, "message": "Invalid data. Lot ID is required."}, status_code=400)
    cost_price = float(data["cost_price"]) if data.get("cost_price") not in (None, "") else None
    supplier = int(data["supplier"]) if data.get("supplier") else None
    exp_raw = str(data.get("expiration_date") or "").strip()[:10]
    try:
        exp_date = date.fromisoformat(exp_raw)
    except ValueError:
        return JSONResponse({"success": False, "message": "Expiration date is invalid."}, status_code=400)
    if exp_date < _manila_today():
        return {"success": False, "message": "Expiration date cannot be in the past."}
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT drug_id FROM inventory_lots WHERE lot_inventory_id = %s", (lot_id,))
                lot_row = cur.fetchone()
                if not lot_row:
                    return JSONResponse({"success": False, "message": "Stock lot not found."}, status_code=404)
                drug_id = int(lot_row["drug_id"])
                sell_price = resolve_selling_price(cur, drug_id, cost_price)
                cur.execute(
                    """
                    UPDATE inventory_lots
                    SET lot_number=%s, expiration_date=%s, current_stock=%s, price=%s, cost_price=%s, supplier=%s
                    WHERE lot_inventory_id=%s
                    """,
                    (
                        str(data.get("lot_number") or "").strip(), exp_raw,
                        int(data.get("current_stock") or 0), sell_price, cost_price, supplier, lot_id,
                    ),
                )
                sync_stock_status_for_drug(cur, drug_id)
                _log(cur, request, "Update Stock Lot", f"Updated stock lot ID {lot_id} ({data.get('lot_number')}).")
        return {"success": True, "message": "Stock lot updated successfully."}
    except IntegrityError:
        return {"success": False, "message": "This lot number already exists for this drug. Please use a different lot/batch number."}


@router.get("/lots/deactivate")
def deactivate_lot(request: Request, id: int = 0):
    if not require_admin(request):
        return _unauthorized()
    if id <= 0:
        return JSONResponse({"success": False, "message": "Lot ID is required."}, status_code=400)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE inventory_lots SET is_active = 0 WHERE lot_inventory_id = %s", (id,))
            _log(cur, request, "Deactivate Stock Lot", f"Stock lot ID {id} has been deactivated.")
    return {"success": True, "message": "Stock lot deactivated successfully."}


@router.get("/lots/reactivate")
def reactivate_lot(request: Request, id: int = 0):
    if not require_admin(request):
        return _unauthorized()
    if id <= 0:
        return JSONResponse({"success": False, "message": "Lot ID is required."}, status_code=400)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE inventory_lots SET is_active = 1 WHERE lot_inventory_id = %s", (id,))
            _log(cur, request, "Reactivate Stock Lot", f"Stock lot ID {id} has been reactivated.")
    return {"success": True, "message": "Stock lot reactivated successfully."}


@router.post("/lots/consignment-cross-check")
def consignment_cross_check(request: Request):
    if not require_admin(request):
        return _unauthorized()
    result = run_consignment_cross_check()
    with get_conn() as conn:
        with conn.cursor() as cur:
            _log(
                cur,
                request,
                "Consignment Cross-Check",
                (
                    f"Updated {result.get('updated', 0)} near-expiry lot(s): "
                    f"{result.get('returnable', 0)} returnable, "
                    f"{result.get('non_returnable', 0)} non-returnable."
                ),
            )
    return result


@router.post("/lots/adjust")
async def adjust_stock(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    lot_id = int(data.get("lot_inventory_id") or 0)
    adj_type = data.get("adjustment_type")
    quantity = data.get("quantity")
    reason = str(data.get("reason") or "").strip()
    notes = str(data.get("notes") or "").strip()
    if not lot_id or adj_type not in {"add", "remove", "set"} or quantity is None or int(quantity) < 0 or not reason:
        return JSONResponse({"success": False, "message": "Missing or invalid fields. Lot, adjustment type, a non-negative quantity, and a reason are all required."}, status_code=400)
    quantity = int(quantity)
    lot = fetch_one("SELECT current_stock, drug_id FROM inventory_lots WHERE lot_inventory_id = %s", (lot_id,))
    if not lot:
        return JSONResponse({"success": False, "message": "Stock lot not found."}, status_code=404)
    previous = int(lot["current_stock"] or 0)
    if adj_type == "add":
        new_stock, change = previous + quantity, quantity
    elif adj_type == "remove":
        new_stock, change = previous - quantity, -quantity
    else:
        new_stock, change = quantity, quantity - previous
    if new_stock < 0:
        return {"success": False, "message": f"That would bring stock below zero (current: {previous}). Please check the quantity."}
    if change == 0:
        return {"success": False, "message": "No change in stock - nothing to adjust."}
    admin_name = request.session.get("user_first_name") or "Admin"
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("UPDATE inventory_lots SET current_stock = %s WHERE lot_inventory_id = %s", (new_stock, lot_id))
            adj_id = next_id(cur, "stock_adjustments", "adjustment_id")
            cur.execute(
                """
                INSERT INTO stock_adjustments
                    (adjustment_id, lot_inventory_id, drug_id, adjustment_type, quantity_change, previous_stock, new_stock, reason, notes, admin_name)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (adj_id, lot_id, lot["drug_id"], adj_type, change, previous, new_stock, reason, notes, admin_name),
            )
            sync_stock_status_for_drug(cur, int(lot["drug_id"]))
            sign = "+" if change > 0 else ""
            _log(cur, request, "Stock Adjustment", f"Lot #{lot_id}: {sign}{change} units ({previous} → {new_stock}). Reason: {reason}.")
    return {"success": True, "new_stock": new_stock, "quantity_change": change}


@router.get("/stock-adjustments")
def stock_adjustments(request: Request, lot_id: int = 0, limit: int = 100):
    if not require_admin(request):
        return _unauthorized()
    limit = max(1, min(500, limit))
    sql = """
        SELECT sa.adjustment_id, sa.lot_inventory_id, sa.drug_id, sa.adjustment_type,
               sa.quantity_change, sa.previous_stock, sa.new_stock, sa.reason, sa.notes,
               sa.admin_name, sa.created_at, d.generic_name, d.brand_name, d.dosage, d.form, l.lot_number
        FROM stock_adjustments sa
        JOIN drugs_master d ON sa.drug_id = d.drug_id
        LEFT JOIN inventory_lots l ON sa.lot_inventory_id = l.lot_inventory_id
    """
    params = []
    if lot_id:
        sql += " WHERE sa.lot_inventory_id = %s"
        params.append(lot_id)
    sql += " ORDER BY sa.created_at DESC LIMIT %s"
    params.append(limit)
    return _rows(fetch_all(sql, tuple(params)))


@router.get("/category-markups")
def get_markups(request: Request):
    if not require_admin(request):
        return _unauthorized()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO category_markup (category, markup_percent)
                SELECT DISTINCT category, 30.00 FROM drugs_master
                WHERE category IS NOT NULL AND category NOT IN (SELECT category FROM category_markup)
                """
            )
            cur.execute("SELECT category, markup_percent, updated_by, updated_at FROM category_markup ORDER BY category ASC")
            rows = cur.fetchall()
    out = []
    for row in rows:
        item = _row(row)
        item["markup_percent"] = float(item.get("markup_percent") or 0)
        out.append(item)
    return out


@router.post("/category-markups")
async def update_markup(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    category = str(data.get("category") or "").strip()
    markup = data.get("markup_percent")
    if not category or markup is None or float(markup) < 0:
        return JSONResponse({"success": False, "message": "Category and a non-negative markup percent are required."}, status_code=400)
    admin_name = actor_display_name(request, fallback="Admin")
    markup_val = float(markup)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO category_markup (category, markup_percent, updated_by, updated_at)
                VALUES (%s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (category) DO UPDATE SET
                    markup_percent = EXCLUDED.markup_percent,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (category, markup_val, admin_name),
            )
            counts = reprice_lots_for_category(cur, category, markup_val)
            details = (
                f"Set {category} markup to {markup_val:.2f}%. "
                f"Repriced {counts['updated']} lot(s)"
            )
            if counts["skipped_no_cost"]:
                details += f"; {counts['skipped_no_cost']} lot(s) skipped (no unit cost)."
            else:
                details += "."
            _log(cur, request, "Update Category Markup", details)
    return {
        "success": True,
        "updated_lots": counts["updated"],
        "skipped_no_cost": counts["skipped_no_cost"],
    }


@router.get("/suppliers")
def list_suppliers(request: Request):
    if not require_admin(request):
        return _unauthorized()
    rows = fetch_all(
        """
        SELECT s.supplier_id, s.supplier_name, s.contact_number, s.email,
               s.address, s.status, s.inactive_reason,
               COALESCE(s.consignment_policy, 'none') AS consignment_policy,
               STRING_AGG(DISTINCT d.generic_name, ', ') AS medicines
        FROM suppliers s
        LEFT JOIN inventory_lots i ON s.supplier_id = i.supplier
        LEFT JOIN drugs_master d ON i.drug_id = d.drug_id
        GROUP BY s.supplier_id
        """
    )
    out = []
    for row in rows:
        item = _row(row)
        meds = item.pop("medicines", None)
        item["medicines_supplied"] = meds.split(", ") if meds else []
        out.append(item)
    return out


def supplier_offered_drug_ids(supplier_id: int) -> list[int]:
    """Drugs this supplier has sold or been ordered for (PO lines + stock lots)."""
    rows = fetch_all(
        """
        SELECT DISTINCT drug_id FROM (
            SELECT poi.drug_id
            FROM purchase_order_items poi
            JOIN purchase_orders po ON po.po_id = poi.po_id
            WHERE po.supplier_id = %s
            UNION
            SELECT il.drug_id
            FROM inventory_lots il
            WHERE il.supplier = %s AND il.drug_id IS NOT NULL
        ) x
        """,
        (supplier_id, supplier_id),
    )
    return [int(r["drug_id"]) for r in rows if r.get("drug_id")]


@router.get("/suppliers/{supplier_id}/ordered-drugs")
def supplier_ordered_drugs(supplier_id: int, request: Request):
    if not require_admin(request):
        return _unauthorized()
    return {"success": True, "drug_ids": supplier_offered_drug_ids(supplier_id)}


@router.post("/suppliers")
async def add_supplier(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    if not data or not data.get("name"):
        return {"success": False, "message": "Invalid input"}
    policy = normalize_consignment_policy(data.get("consignment_policy"))
    with get_conn() as conn:
        with conn.cursor() as cur:
            sid = next_id(cur, "suppliers", "supplier_id")
            cur.execute(
                """
                INSERT INTO suppliers (supplier_id, supplier_name, contact_number, email, address, consignment_policy)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (sid, data.get("name"), data.get("contact"), data.get("email"), data.get("address"), policy),
            )
            _log(cur, request, "Add Supplier", f"Added new supplier: {data.get('name')}")
    return {"success": True}


@router.post("/suppliers/update")
async def update_supplier(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    if not data or not data.get("supplier_id") or not data.get("name"):
        return {"success": False, "message": "Invalid input"}
    supplier_id = int(data["supplier_id"])
    name = str(data["name"]).strip()
    status = "Inactive" if data.get("status") == "Inactive" else "Active"
    inactive_reason = str(data.get("inactive_reason") or "").strip()
    if status == "Active":
        inactive_reason = None
    elif not inactive_reason:
        inactive_reason = "Manually deactivated by admin"
    policy = normalize_consignment_policy(data.get("consignment_policy"))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE suppliers
                SET supplier_name=%s, contact_number=%s, email=%s, address=%s, status=%s, inactive_reason=%s, consignment_policy=%s
                WHERE supplier_id=%s
                """,
                (name, data.get("contact") or "", data.get("email") or "", data.get("address") or "", status, inactive_reason, policy, supplier_id),
            )
            extra = f" - Reason: {inactive_reason}" if status == "Inactive" else ""
            _log(cur, request, "Edit Supplier", f"Updated supplier: {name} (ID {supplier_id}){extra}")
    return {"success": True}


@router.get("/staff")
def list_staff(request: Request):
    if not require_admin(request):
        return _unauthorized()
    return _rows(fetch_all(
        """
        SELECT u.user_id, r.role_name, s.first_name, s.middle_name, s.last_name, s.email, s.phone_number,
               COALESCE(u.is_active, 1) AS is_active
        FROM users u
        JOIN staff_info s ON u.user_id = s.user_id
        JOIN role r ON u.role_id = r.role_id
        WHERE LOWER(r.role_name) != 'customer'
        ORDER BY u.is_active DESC, u.user_id DESC
        """
    ))


@router.get("/customers")
def list_customers(request: Request):
    if not require_admin(request):
        return _unauthorized()
    return _rows(fetch_all(
        """
        SELECT customer_id, first_name, middle_name, last_name, email, phone_number, customer_type, loyalty_points,
               COALESCE(is_active, 1) AS is_active
        FROM customers ORDER BY is_active DESC, customer_id DESC
        """
    ))


@router.post("/staff/one")
async def staff_one(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await _body(request)
    user_id = int(data.get("user_id") or 0)
    if not user_id:
        return {"success": False, "message": "Invalid request."}
    row = fetch_one(
        """
        SELECT u.user_id, r.role_name, s.first_name, s.middle_name, s.last_name,
               s.email, s.phone_number, s.address, u.username
        FROM users u
        JOIN staff_info s ON u.user_id = s.user_id
        JOIN role r ON u.role_id = r.role_id
        WHERE u.user_id = %s
        """,
        (user_id,),
    )
    if not row:
        return {"success": False, "message": "Staff not found or database error."}
    return {"success": True, "data": _row(row)}


@router.post("/customers/one")
async def customer_one(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await _body(request)
    raw_cid = data.get("customer_id")
    if raw_cid in (None, ""):
        return {"success": False, "message": "Invalid request."}
    customer_id = int(raw_cid)
    row = fetch_one(
        """
        SELECT customer_id, first_name, middle_name, last_name, username, address, phone_number,
               customer_type, loyalty_points, email
        FROM customers WHERE customer_id = %s
        """,
        (customer_id,),
    )
    if not row:
        return {"success": False, "message": "Customer not found or database error."}
    return {"success": True, "data": _row(row)}


@router.post("/staff")
async def update_staff(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await _body(request)
    user_id = int(data.get("user_id") or 0)
    first_name = str(data.get("first_name") or "").strip()
    last_name = str(data.get("last_name") or "").strip()
    middle_name = str(data.get("middle_name") or "").strip()
    email = str(data.get("email") or "").strip()
    phone = str(data.get("phone_number") or "").strip()
    address = str(data.get("address") or "").strip()
    role = str(data.get("role") or "").strip()
    if user_id <= 0:
        return {"success": False, "message": "Invalid request."}
    profile_err, packed = prepare_profile_fields(first_name, last_name, email, phone, address, middle_name)
    if profile_err:
        return {"success": False, "message": profile_err}
    first_name, last_name, middle_name = packed["first_name"], packed["last_name"], packed["middle_name"]
    email, phone, address = packed["email"], packed["phone_number"], packed["address"]
    if role not in ("Admin", "Cashier/Pharmacist"):
        return {"success": False, "message": "Select a valid role."}
    role_row = fetch_one("SELECT role_id FROM role WHERE role_name = %s", (role,))
    if not role_row:
        return {"success": False, "message": f"Role '{role}' does not exist."}
    password = data.get("password") or ""
    if password:
        pw_err = password_complexity_error(password)
        if pw_err:
            return {"success": False, "message": pw_err}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE staff_info SET first_name=%s, middle_name=%s, last_name=%s, email=%s, phone_number=%s, address=%s
                WHERE user_id=%s
                """,
                (
                    first_name, middle_name, last_name, email, phone, address, user_id,
                ),
            )
            if password:
                cur.execute("UPDATE users SET role_id=%s, password=%s WHERE user_id=%s", (role_row["role_id"], hash_password(password), user_id))
            else:
                cur.execute("UPDATE users SET role_id=%s WHERE user_id=%s", (role_row["role_id"], user_id))
            _log(cur, request, "Update Staff", f"Updated staff ID {user_id} ({first_name} {last_name}).")
    return {"success": True, "message": "Staff updated successfully."}


@router.post("/customers")
async def update_customer(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await _body(request)
    raw_cid = data.get("customer_id")
    customer_id = int(raw_cid) if raw_cid not in (None, "") else None
    first_name = str(data.get("first_name") or "").strip()
    last_name = str(data.get("last_name") or "").strip()
    middle_name = str(data.get("middle_name") or "").strip()
    email = str(data.get("email") or "").strip()
    phone = str(data.get("phone_number") or "").strip()
    address = str(data.get("address") or "").strip()
    customer_type = str(data.get("customer_type") or "Regular").strip()
    if customer_id is None or customer_id < 0:
        return {"success": False, "message": "Invalid request."}
    profile_err, packed = prepare_profile_fields(first_name, last_name, email, phone, address, middle_name)
    if profile_err:
        return {"success": False, "message": profile_err}
    first_name, last_name, middle_name = packed["first_name"], packed["last_name"], packed["middle_name"]
    email, phone, address = packed["email"], packed["phone_number"], packed["address"]
    if customer_type not in CUSTOMER_TYPES:
        return {"success": False, "message": "Select a valid customer type."}
    try:
        loyalty_points = float(data.get("loyalty_points") or 0)
    except (TypeError, ValueError):
        return {"success": False, "message": "Points must be a number."}
    if loyalty_points < 0:
        return {"success": False, "message": "Points cannot be negative."}
    email_taken = fetch_one(
        "SELECT customer_id FROM customers WHERE LOWER(email) = LOWER(%s) AND customer_id != %s",
        (email, customer_id),
    )
    if email_taken:
        return {"success": False, "message": "Email address is already registered."}
    password = data.get("password") or ""
    if password:
        pw_err = password_complexity_error(password)
        if pw_err:
            return {"success": False, "message": pw_err}
    fields = (
        first_name, middle_name, last_name, email, phone, address, customer_type, loyalty_points,
    )
    with get_conn() as conn:
        with conn.cursor() as cur:
            if password:
                cur.execute(
                    """
                    UPDATE customers SET first_name=%s, middle_name=%s, last_name=%s, email=%s, phone_number=%s,
                        address=%s, customer_type=%s, loyalty_points=%s, password=%s WHERE customer_id=%s
                    """,
                    (*fields, hash_password(password), customer_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE customers SET first_name=%s, middle_name=%s, last_name=%s, email=%s, phone_number=%s,
                        address=%s, customer_type=%s, loyalty_points=%s WHERE customer_id=%s
                    """,
                    (*fields, customer_id),
                )
            _log(cur, request, "Update Customer", f"Updated customer ID {customer_id} ({first_name} {last_name}).")
    return {"success": True, "message": "Customer updated successfully."}


def _active_admin_count():
    row = fetch_one(
        """
        SELECT COUNT(*) AS n
        FROM users u
        JOIN role r ON u.role_id = r.role_id
        WHERE u.is_active = 1 AND LOWER(r.role_name) = 'admin'
        """
    )
    return int(row["n"] if row else 0)


@router.post("/staff/deactivate")
async def deactivate_staff(request: Request):
    admin_id = require_admin(request)
    if not admin_id:
        return _unauthorized()
    data = await _body(request)
    user_id = int(data.get("user_id") or 0)
    if not user_id:
        return {"success": False, "message": "Invalid request."}
    if user_id == int(admin_id):
        return {"success": False, "message": "You cannot deactivate your own account."}
    staff = fetch_one(
        """
        SELECT r.role_name FROM users u
        JOIN role r ON u.role_id = r.role_id
        WHERE u.user_id = %s
        """,
        (user_id,),
    )
    if not staff:
        return {"success": False, "message": "Staff account not found."}
    if str(staff.get("role_name") or "").lower() == "admin" and _active_admin_count() <= 1:
        return {"success": False, "message": "Cannot deactivate the last active admin."}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_active = 0 WHERE user_id = %s", (user_id,))
            _log(cur, request, "Deactivate Staff", f"Staff account ID {user_id} has been deactivated.")
    return {"success": True, "message": "Staff account successfully deactivated."}


@router.post("/staff/activate")
async def activate_staff(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await _body(request)
    user_id = int(data.get("user_id") or 0)
    if not user_id:
        return {"success": False, "message": "Invalid request."}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_active = 1 WHERE user_id = %s", (user_id,))
            _log(cur, request, "Activate Staff", f"Staff account ID {user_id} has been activated.")
    return {"success": True, "message": "Staff account successfully activated."}


@router.post("/customers/deactivate")
async def deactivate_customer(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await _body(request)
    raw_cid = data.get("customer_id")
    if raw_cid in (None, ""):
        return {"success": False, "message": "Invalid request."}
    customer_id = int(raw_cid)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE customers SET is_active = 0 WHERE customer_id = %s", (customer_id,))
            cur.execute("UPDATE users SET is_active = 0 WHERE user_id = %s", (customer_id,))
            _log(cur, request, "Deactivate Customer", f"Customer ID {customer_id} was deactivated.")
    return {"success": True, "message": "Customer account successfully deactivated."}


@router.post("/customers/activate")
async def activate_customer(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await _body(request)
    raw_cid = data.get("customer_id")
    if raw_cid in (None, ""):
        return {"success": False, "message": "Invalid request."}
    customer_id = int(raw_cid)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE customers SET is_active = 1 WHERE customer_id = %s", (customer_id,))
            cur.execute("UPDATE users SET is_active = 1 WHERE user_id = %s", (customer_id,))
            _log(cur, request, "Activate Customer", f"Customer ID {customer_id} was activated.")
    return {"success": True, "message": "Customer account successfully activated."}


@router.post("/staff/reset-password")
async def reset_staff_password(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    user_id = int(data.get("user_id") or 0)
    if not user_id:
        return {"success": False, "message": "Missing user_id."}
    staff = fetch_one(
        """
        SELECT u.user_id, u.username, r.role_name, s.first_name, s.last_name
        FROM users u
        JOIN role r ON u.role_id = r.role_id
        LEFT JOIN staff_info s ON u.user_id = s.user_id
        WHERE u.user_id = %s AND u.is_active = 1
        """,
        (user_id,),
    )
    if not staff:
        return {"success": False, "message": "Staff account not found or inactive."}
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    temp = "".join(alphabet[random.randint(0, len(alphabet) - 1)] for _ in range(10))
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET password = %s WHERE user_id = %s", (hash_password(temp), user_id))
            staff_name = f"{staff.get('first_name') or ''} {staff.get('last_name') or ''}".strip() or staff["username"]
            _log(cur, request, "Reset Staff Password", f"Reset password for {staff_name} ({staff['role_name']}, username: {staff['username']}).")
    return {
        "success": True,
        "username": staff["username"],
        "staff_name": f"{staff.get('first_name') or ''} {staff.get('last_name') or ''}".strip() or staff["username"],
        "temp_password": temp,
    }


@router.post("/users")
async def user_actions(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await _body(request)
    if str(data.get("action") or "") != "add":
        return {"status": "error", "msg": "Unknown action."}
    role = str(data.get("role") or "").strip()
    first_name = str(data.get("first_name") or "").strip()
    last_name = str(data.get("last_name") or "").strip()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    middle = str(data.get("middle_name") or "").strip()
    email = str(data.get("email") or "").strip()
    phone = str(data.get("phone_number") or "").strip()
    address = str(data.get("address") or "").strip()
    user_err = validate_username(username)
    if user_err:
        return {"status": "error", "msg": user_err}
    profile_err, packed = prepare_profile_fields(first_name, last_name, email, phone, address, middle)
    if profile_err:
        return {"status": "error", "msg": profile_err}
    first_name, last_name, middle = packed["first_name"], packed["last_name"], packed["middle_name"]
    email, phone, address = packed["email"], packed["phone_number"], packed["address"]
    pw_err = password_complexity_error(password)
    if pw_err:
        return {"status": "error", "msg": pw_err}
    if role == "Customer":
        customer_type = str(data.get("customer_type") or "Regular").strip()
        if customer_type not in CUSTOMER_TYPES:
            return {"status": "error", "msg": "Select a valid customer type."}
        try:
            loyalty_points = float(data.get("loyalty_points") or 0)
        except (TypeError, ValueError):
            return {"status": "error", "msg": "Points must be a number."}
        if loyalty_points < 0:
            return {"status": "error", "msg": "Points cannot be negative."}
        if fetch_one("SELECT customer_id FROM customers WHERE LOWER(email) = LOWER(%s)", (email,)):
            return {"status": "error", "msg": "Email address is already registered."}
    elif role not in ("Admin", "Cashier/Pharmacist"):
        return {"status": "error", "msg": "Select a valid role."}
    if fetch_one("SELECT user_id FROM users WHERE username = %s", (username,)) or fetch_one(
        "SELECT customer_id FROM customers WHERE username = %s", (username,)
    ):
        return {"status": "error", "msg": "Username is already taken."}
    hashed = hash_password(password)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if role == "Customer":
                cid = next_id(cur, "customers", "customer_id")
                uid = next_id(cur, "users", "user_id")
                new_id = max(cid, uid)
                cur.execute(
                    "INSERT INTO users (user_id, username, password, role_id, is_active) VALUES (%s, %s, %s, 3, 1)",
                    (new_id, username, hashed),
                )
                cur.execute(
                    """
                    INSERT INTO customers
                        (customer_id, first_name, middle_name, last_name, username, address, phone_number,
                         customer_type, password, loyalty_points, email)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        new_id, first_name, middle, last_name, username, address, phone,
                        customer_type, hashed, loyalty_points, email,
                    ),
                )
                _log(cur, request, "Add Customer", f"Added customer {first_name} {last_name} (ID {new_id}).")
                return {"status": "success", "user_id": new_id}
            role_row = fetch_one("SELECT role_id FROM role WHERE role_name = %s", (role,))
            if not role_row:
                return {"status": "error", "msg": f"Role '{role}' does not exist."}
            user_id = next_id(cur, "users", "user_id")
            cur.execute(
                "INSERT INTO users (user_id, username, password, role_id, is_active) VALUES (%s, %s, %s, %s, 1)",
                (user_id, username, hashed, role_row["role_id"]),
            )
            staff_id = next_id(cur, "staff_info", "staff_id")
            cur.execute(
                """
                INSERT INTO staff_info (staff_id, user_id, first_name, middle_name, last_name, email, phone_number, address)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (staff_id, user_id, first_name, middle, last_name, email, phone, address),
            )
            _log(cur, request, "Add Staff", f"Added staff {first_name} {last_name} ({role}, ID {user_id}).")
            return {"status": "success", "user_id": user_id}

