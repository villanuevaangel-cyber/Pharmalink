import json
import os
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from psycopg2.extras import RealDictCursor

from app.activity import write_activity_log
from app.db import fetch_all, fetch_one, get_conn, next_id
from app.deps import session_user_id
from app.payments import normalize_payment_method
from app.paymongo import PayMongoError, create_checkout_session, require_paid_checkout
from app.stock import match_prescription_to_stock, sync_stock_status_for_drug
from app.profile_photos import (
    ALLOWED_EXT,
    customer_photo_url,
    photo_response,
    resolve_photo_url,
    store_customer_photo,
)
from app.validation import prepare_profile_fields

router = APIRouter(prefix="/api/customer", tags=["customer"])

ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_AVATAR = "https://cdn-icons-png.flaticon.com/512/2922/2922510.png"


def require_customer(request: Request):
    user_id = session_user_id(request)
    if user_id is None or str(request.session.get("user_role", "")).lower() != "customer":
        return None
    return user_id


def _fmt_money(value) -> str:
    return f"{float(value or 0):.2f}"


def _fmt_dt(value, fmt: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value.strftime(fmt)


@router.get("/home-stats")
def home_stats(request: Request):
    customer_id = require_customer(request)
    if customer_id is None:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)

    orders = fetch_one(
        """
        SELECT COUNT(order_id) AS total_orders, SUM(total_amount) AS total_spent
        FROM customer_orders
        WHERE customer_id = %s AND order_status IN ('Completed', 'Delivered')
        """,
        (customer_id,),
    )
    walkin = fetch_one(
        """
        SELECT COUNT(sale_id) AS total_orders, SUM(total_amount) AS total_spent
        FROM sales
        WHERE customer_id = %s AND status = 'completed'
        """,
        (customer_id,),
    )
    points = fetch_one("SELECT loyalty_points FROM customers WHERE customer_id = %s", (customer_id,))
    recent = fetch_all(
        """
        SELECT order_id, order_date, order_status, total_amount, payment_method, kind FROM (
            SELECT order_id, order_date, order_status, total_amount,
                   COALESCE(payment_method, 'cash') AS payment_method, 'online'::text AS kind
            FROM customer_orders WHERE customer_id = %s
            UNION ALL
            SELECT sale_id, date_created, INITCAP(status), total_amount,
                   COALESCE(payment_method, 'cash'), 'walkin'::text
            FROM sales WHERE customer_id = %s AND status = 'completed'
        ) combined
        ORDER BY order_date DESC
        LIMIT 20
        """,
        (customer_id, customer_id),
    )
    transaction_logs = [
        {
            "order_id": int(r["order_id"]),
            "order_date": _fmt_dt(r["order_date"], "%b %d, %Y %I:%M %p"),
            "order_status": r["order_status"],
            "total_amount": _fmt_money(r["total_amount"]),
            "payment_method": r.get("payment_method") or "cash",
            "kind": r.get("kind") or "online",
        }
        for r in recent
    ]
    item_rows = fetch_all(
        """
        SELECT item_name, generic_name, SUM(qty) AS total_qty, SUM(line_total) AS total_spent,
               MAX(order_date) AS last_ordered
        FROM (
            SELECT COALESCE(NULLIF(TRIM(dm.brand_name), ''), dm.generic_name) AS item_name,
                   dm.generic_name,
                   od.quantity AS qty,
                   (od.quantity * od.price_per_unit) AS line_total,
                   co.order_date
            FROM order_details od
            JOIN customer_orders co ON od.order_id = co.order_id
            JOIN drugs_master dm ON od.drug_id = dm.drug_id
            WHERE co.customer_id = %s
            UNION ALL
            SELECT COALESCE(NULLIF(TRIM(dm.brand_name), ''), dm.generic_name),
                   dm.generic_name,
                   si.quantity,
                   (si.quantity * si.price),
                   s.date_created
            FROM sales_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN drugs_master dm ON si.drug_id = dm.drug_id
            WHERE s.customer_id = %s AND s.status = 'completed'
        ) purchased
        GROUP BY item_name, generic_name
        ORDER BY SUM(qty) DESC, MAX(order_date) DESC
        LIMIT 10
        """,
        (customer_id, customer_id),
    )
    ordered_items = [
        {
            "name": r["item_name"] or r["generic_name"] or "Item",
            "generic_name": r.get("generic_name") or "",
            "qty": int(r["total_qty"] or 0),
            "spent": _fmt_money(r["total_spent"]),
            "last_ordered": _fmt_dt(r["last_ordered"], "%b %d, %Y"),
        }
        for r in item_rows
    ]
    online_count = int(orders["total_orders"] or 0) if orders else 0
    online_spent = float(orders["total_spent"] or 0) if orders else 0
    walkin_count = int(walkin["total_orders"] or 0) if walkin else 0
    walkin_spent = float(walkin["total_spent"] or 0) if walkin else 0
    return {
        "success": True,
        "total_orders": online_count + walkin_count,
        "total_spent": _fmt_money(online_spent + walkin_spent),
        "loyalty_points": points["loyalty_points"] if points else 0,
        "available_vouchers": 0,
        "recent_orders": transaction_logs,
        "transaction_logs": transaction_logs,
        "ordered_items": ordered_items,
    }


@router.get("/profile")
def get_profile(request: Request):
    customer_id = require_customer(request)
    if customer_id is None:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)
    customer = fetch_one(
        """
        SELECT first_name, middle_name, last_name, email, phone_number, address,
               customer_type, loyalty_points, profile_image, username,
               (profile_image_data IS NOT NULL) AS has_profile_photo
        FROM customers WHERE customer_id = %s
        """,
        (customer_id,),
    )
    if not customer:
        return {"success": False, "message": "Customer not found."}
    customer = dict(customer)
    customer["loyalty_points"] = _fmt_money(customer.get("loyalty_points"))
    has_photo = bool(customer.pop("has_profile_photo", False))
    customer["profile_image"] = resolve_photo_url(
        customer.get("profile_image"), has_photo, customer_photo_url(customer_id)
    )
    return {"success": True, "data": customer}


@router.post("/profile")
async def update_profile(request: Request):
    customer_id = require_customer(request)
    if customer_id is None:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)

    form = await request.form()
    err, packed = prepare_profile_fields(
        str(form.get("first_name") or ""),
        str(form.get("last_name") or ""),
        str(form.get("email") or ""),
        str(form.get("phone_number") or ""),
        str(form.get("address") or ""),
        str(form.get("middle_name") or ""),
    )
    if err:
        return {"success": False, "message": err}
    first_name = packed["first_name"]
    middle_name = packed["middle_name"]
    last_name = packed["last_name"]
    email = packed["email"]
    phone_number = packed["phone_number"]
    address = packed["address"]

    profile_image_path = None
    upload = form.get("profile_image")
    if upload and getattr(upload, "filename", ""):
        ext = Path(upload.filename).suffix.lower().lstrip(".")
        if ext not in ALLOWED_EXT:
            return {"success": False, "message": "Only JPG, PNG, WEBP, or GIF images are allowed."}
        content = await upload.read()
        if len(content) > 3 * 1024 * 1024:
            return {"success": False, "message": "Image must be under 3MB."}
        profile_image_path = store_customer_photo(customer_id, content, ext)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            if profile_image_path:
                cur.execute(
                    """
                    UPDATE customers SET first_name=%s, middle_name=%s, last_name=%s, email=%s,
                           phone_number=%s, address=%s, profile_image=%s
                    WHERE customer_id=%s
                    """,
                    (first_name, middle_name, last_name, email, phone_number, address, profile_image_path, customer_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE customers SET first_name=%s, middle_name=%s, last_name=%s, email=%s,
                           phone_number=%s, address=%s
                    WHERE customer_id=%s
                    """,
                    (first_name, middle_name, last_name, email, phone_number, address, customer_id),
                )
            cur.execute(
                """
                SELECT first_name, middle_name, last_name, email, phone_number, address,
                       customer_type, loyalty_points, profile_image, username
                FROM customers WHERE customer_id = %s
                """,
                (customer_id,),
            )
            customer = cur.fetchone()
            photo = " (including profile picture)" if profile_image_path else ""
            write_activity_log(
                cur,
                "Update Profile",
                f"Updated customer profile{photo}.",
                request=request,
                actor=f"{first_name} {last_name}".strip(),
            )

    if not customer:
        return {"success": False, "message": "Save did not persist correctly. Please try again or contact support."}

    customer = dict(customer)
    matches = (
        customer["first_name"] == first_name
        and (customer["middle_name"] or "") == middle_name
        and customer["last_name"] == last_name
        and customer["email"] == email
        and (customer["phone_number"] or "") == phone_number
        and (customer["address"] or "") == address
    )
    if not matches:
        return {"success": False, "message": "Save did not persist correctly. Please try again or contact support."}

    request.session["user_first_name"] = customer["first_name"]
    request.session["user_last_name"] = customer.get("last_name") or ""
    return {
        "success": True,
        "message": "Profile updated successfully.",
        "profile_image": customer.get("profile_image") or profile_image_path or "",
        "customer": customer,
    }


@router.get("/profile-photo")
def get_profile_photo(request: Request):
    customer_id = require_customer(request)
    if customer_id is None:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)
    row = fetch_one(
        "SELECT profile_image, profile_image_data, profile_image_mime FROM customers WHERE customer_id = %s",
        (customer_id,),
    )
    if not row:
        return JSONResponse({"success": False, "message": "Not found."}, status_code=404)
    image = photo_response(row.get("profile_image_data"), row.get("profile_image_mime"), row.get("profile_image"))
    if image is None:
        return JSONResponse({"success": False, "message": "No photo."}, status_code=404)
    return image


@router.post("/profile-picture")
async def profile_picture(request: Request, profile_image: UploadFile = File(...)):
    customer_id = require_customer(request)
    if customer_id is None:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)
    if not profile_image.filename:
        return {"success": False, "message": "Please choose an image to upload."}
    ext = Path(profile_image.filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED_EXT:
        return {"success": False, "message": "Only JPG, PNG, WEBP, or GIF images are allowed."}
    content = await profile_image.read()
    if len(content) > 3 * 1024 * 1024:
        return {"success": False, "message": "Image must be under 3MB."}
    path = store_customer_photo(customer_id, content, ext)
    with get_conn() as conn:
        with conn.cursor() as cur:
            write_activity_log(cur, "Update Profile Picture", "Uploaded a new profile picture.", request=request)
    return {"success": True, "path": path}


@router.get("/orders")
def list_orders(request: Request, type: str = "online", start_date: str = "", end_date: str = ""):
    customer_id = require_customer(request)
    if customer_id is None:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)

    rows = []
    if type == "walkin":
        sql = """
            SELECT sale_id AS order_id, date_created AS order_date, total_amount, status AS order_status, payment_method
            FROM sales WHERE customer_id = %s AND status = 'completed'
        """
        params = [customer_id]
        if start_date and end_date:
            sql += " AND date_created::date BETWEEN %s AND %s"
            params.extend([start_date, end_date])
        sql += " ORDER BY date_created DESC"
        for r in fetch_all(sql, params):
            rows.append({
                "order_id": int(r["order_id"]),
                "order_date": _fmt_dt(r["order_date"], "%b %d, %Y %I:%M %p"),
                "total_amount": _fmt_money(r["total_amount"]),
                "order_status": str(r["order_status"]).capitalize(),
                "payment_method": r.get("payment_method") or "cash",
                "kind": "walkin",
            })
    else:
        sql = "SELECT order_id, order_date, total_amount, order_status, payment_method FROM customer_orders WHERE customer_id = %s"
        params = [customer_id]
        if start_date and end_date:
            sql += " AND order_date::date BETWEEN %s AND %s"
            params.extend([start_date, end_date])
        sql += " ORDER BY order_date DESC"
        for r in fetch_all(sql, params):
            rows.append({
                "order_id": int(r["order_id"]),
                "order_date": _fmt_dt(r["order_date"], "%b %d, %Y %I:%M %p"),
                "total_amount": _fmt_money(r["total_amount"]),
                "order_status": r["order_status"],
                "payment_method": r.get("payment_method") or "cash",
                "kind": "online",
            })
    return {"success": True, "orders": rows}


@router.get("/orders/{order_id}")
def order_details(order_id: int, request: Request, kind: str = "online"):
    customer_id = require_customer(request)
    if customer_id is None:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)

    customer = fetch_one(
        "SELECT first_name, middle_name, last_name, loyalty_points FROM customers WHERE customer_id = %s",
        (customer_id,),
    )
    parts = []
    if customer:
        parts = [str(customer.get("first_name") or "").strip(), str(customer.get("middle_name") or "").strip(), str(customer.get("last_name") or "").strip()]
    customer_name = " ".join(p for p in parts if p) or "Customer N/A"
    loyalty = float(customer["loyalty_points"]) if customer else 0

    if str(kind).lower() == "walkin":
        sale = fetch_one(
            """
            SELECT sale_id, customer_id, status, payment_method, total_amount
            FROM sales WHERE sale_id = %s AND status = 'completed'
            """,
            (order_id,),
        )
        if not sale:
            return JSONResponse({"success": False, "message": "Sale not found."}, status_code=404)
        if sale["customer_id"] is None or int(sale["customer_id"]) != customer_id:
            return JSONResponse({"success": False, "message": "Unauthorized access."}, status_code=403)
        items = fetch_all(
            """
            SELECT si.drug_id, si.lot_id AS lot_inventory_id, si.quantity AS ordered_qty,
                   si.price AS price_per_unit, dm.brand_name, dm.generic_name, dm.dosage,
                   COALESCE(il.current_stock, 0) AS current_stock
            FROM sales_items si
            JOIN drugs_master dm ON si.drug_id = dm.drug_id
            LEFT JOIN inventory_lots il ON si.lot_id = il.lot_inventory_id
            WHERE si.sale_id = %s
            """,
            (order_id,),
        )
        return {
            "success": True,
            "order_id": order_id,
            "kind": "walkin",
            "customer_name": customer_name,
            "customer_id": customer_id,
            "loyalty_points": loyalty,
            "status": str(sale["status"]).capitalize(),
            "payment_method": sale.get("payment_method") or "cash",
            "items": [dict(i) for i in items],
        }

    header = fetch_one(
        "SELECT customer_id, order_status FROM customer_orders WHERE order_id = %s",
        (order_id,),
    )
    if not header:
        return JSONResponse({"success": False, "message": "Order not found."}, status_code=404)
    if int(header["customer_id"]) != customer_id:
        return JSONResponse({"success": False, "message": "Unauthorized access."}, status_code=403)

    items = fetch_all(
        """
        SELECT od.drug_id, od.lot_inventory_id, od.quantity AS ordered_qty, od.price_per_unit,
               dm.brand_name, dm.generic_name, dm.dosage, il.current_stock
        FROM order_details od
        JOIN drugs_master dm ON od.drug_id = dm.drug_id
        JOIN inventory_lots il ON od.lot_inventory_id = il.lot_inventory_id
        WHERE od.order_id = %s
        """,
        (order_id,),
    )
    return {
        "success": True,
        "order_id": order_id,
        "kind": "online",
        "customer_name": customer_name,
        "customer_id": header["customer_id"],
        "loyalty_points": loyalty,
        "status": header["order_status"],
        "items": [dict(i) for i in items],
    }


def _paymongo_base_url() -> str:
    return (os.getenv("PAYMONGO_BASE_URL") or "http://127.0.0.1:8080").rstrip("/")


def _quote_online_total(items) -> float:
    server_total = 0.0
    for item in items:
        lot_id = int(item.get("lot_id") or 0)
        qty = int(item.get("quantity") or 0)
        if lot_id <= 0 or qty <= 0:
            raise ValueError("Invalid item in cart.")
        lot = fetch_one(
            "SELECT current_stock, price FROM inventory_lots WHERE lot_inventory_id = %s AND is_active = 1",
            (lot_id,),
        )
        if not lot:
            raise ValueError("One of the items in your cart is no longer available.")
        if int(lot["current_stock"]) < qty:
            raise ValueError(
                f"Not enough stock left for one of your items (only {lot['current_stock']} available). Please update your cart."
            )
        server_total += float(lot["price"] or item.get("price_per_unit") or 0) * qty
    return round(server_total, 2)


@router.post("/ewallet/checkout")
async def customer_ewallet_checkout(request: Request):
    customer_id = require_customer(request)
    if customer_id is None:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)
    payload = await request.json()
    items = payload.get("items") or []
    if not items:
        return JSONResponse({"success": False, "message": "Your cart is empty."}, status_code=400)
    method = str(payload.get("payment_method") or "").strip().lower()
    if method not in ("gcash", "maya"):
        return JSONResponse({"success": False, "message": "Choose GCash or Maya."}, status_code=400)
    order_token = str(payload.get("order_token") or "").strip()
    if not order_token or order_token == "no_token":
        return JSONResponse({"success": False, "message": "Missing order token. Please refresh the page and try again."}, status_code=400)
    try:
        amount = _quote_online_total(items)
        session = create_checkout_session(
            amount=amount,
            channel=method,
            description=f"PharmaLink order {method.upper()}",
            success_url=f"{_paymongo_base_url()}/api/customer/ewallet/complete",
            cancel_url=f"{_paymongo_base_url()}/customer/customer.html#products",
            metadata={"kind": "online", "customer_id": str(customer_id), "method": method},
        )
    except PayMongoError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=exc.status_code)
    except ValueError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=409)
    request.session["ewallet_items"] = items
    request.session["ewallet_token"] = order_token
    request.session["ewallet_method"] = method
    request.session["ewallet_checkout_id"] = session["id"]
    request.session["ewallet_amount"] = amount
    return {"success": True, "checkout_url": session["checkout_url"], "id": session["id"], "amount": amount}


@router.get("/ewallet/complete")
async def customer_ewallet_complete(request: Request):
    customer_id = require_customer(request)
    if customer_id is None:
        return RedirectResponse(url="/", status_code=302)
    checkout_id = str(request.session.get("ewallet_checkout_id") or "").strip()
    items = request.session.get("ewallet_items") or []
    order_token = str(request.session.get("ewallet_token") or "").strip()
    method = str(request.session.get("ewallet_method") or "").strip().lower()
    if not checkout_id or not items or method not in ("gcash", "maya"):
        return RedirectResponse(url="/customer/customer.html#products", status_code=302)
    placed = await place_order_with_payload(request, customer_id, {
        "items": items,
        "order_token": order_token,
        "payment_method": method,
        "paymongo_checkout_id": checkout_id,
    })
    for key in ("ewallet_items", "ewallet_token", "ewallet_method", "ewallet_checkout_id", "ewallet_amount"):
        request.session.pop(key, None)
    if isinstance(placed, dict) and placed.get("success"):
        return RedirectResponse(url="/customer/customer.html#orders", status_code=302)
    if isinstance(placed, JSONResponse) and placed.status_code in (200, 409):
        return RedirectResponse(url="/customer/customer.html#orders", status_code=302)
    return RedirectResponse(url="/customer/customer.html#products", status_code=302)


@router.post("/orders")
async def place_order(request: Request):
    customer_id = require_customer(request)
    if customer_id is None:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)
    payload = await request.json()
    result = await place_order_with_payload(request, customer_id, payload)
    if isinstance(result, JSONResponse):
        return result
    return result


async def place_order_with_payload(request: Request, customer_id: int, payload: dict):

    exists = fetch_one("SELECT customer_id FROM customers WHERE customer_id = %s", (customer_id,))
    if not exists:
        request.session.clear()
        return JSONResponse({"success": False, "message": "Your session is out of date. Please log in again."}, status_code=401)

    items = payload.get("items") or []
    if not items:
        return JSONResponse({"success": False, "message": "Your cart is empty."}, status_code=400)
    order_token = str(payload.get("order_token") or "").strip()
    if not order_token or order_token == "no_token":
        return JSONResponse(
            {"success": False, "message": "Missing order token. Please refresh the page and try again."},
            status_code=400,
        )

    dup = fetch_one("SELECT order_id FROM customer_orders WHERE order_token = %s LIMIT 1", (order_token,))
    if dup:
        return JSONResponse({"success": True, "order_id": dup["order_id"], "duplicate": True}, status_code=409)

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                server_total = 0.0
                affected = set()
                for item in items:
                    lot_id = int(item.get("lot_id") or 0)
                    qty = int(item.get("quantity") or 0)
                    if lot_id <= 0 or qty <= 0:
                        raise ValueError("Invalid item in cart.")
                    cur.execute(
                        "SELECT current_stock, price FROM inventory_lots WHERE lot_inventory_id = %s AND is_active = 1 FOR UPDATE",
                        (lot_id,),
                    )
                    lot = cur.fetchone()
                    if not lot:
                        raise ValueError("One of the items in your cart is no longer available.")
                    if int(lot["current_stock"]) < qty:
                        raise ValueError(
                            f"Not enough stock left for one of your items (only {lot['current_stock']} available). Please update your cart."
                        )
                    unit_price = float(lot["price"] or item.get("price_per_unit") or 0)
                    server_total += unit_price * qty

                payment_method = normalize_payment_method(payload.get("payment_method"), "cash") or "cash"
                if payment_method in ("gcash", "maya"):
                    checkout_id = str(payload.get("paymongo_checkout_id") or request.session.get("ewallet_checkout_id") or "").strip()
                    paid = require_paid_checkout(checkout_id, server_total, payment_method)
                    payment_reference = paid.get("reference") or checkout_id
                else:
                    payment_reference = None

                order_id = next_id(cur, "customer_orders", "order_id")
                cur.execute("SAVEPOINT order_header")
                try:
                    cur.execute(
                        """
                        INSERT INTO customer_orders
                            (order_id, customer_id, order_date, order_status, total_amount, is_read, order_token, payment_method)
                        VALUES (%s, %s, NOW(), 'Pending', %s, 0, %s, %s)
                        """,
                        (order_id, customer_id, server_total, order_token, payment_method),
                    )
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT order_header")
                    cur.execute(
                        """
                        INSERT INTO customer_orders (order_id, customer_id, order_date, order_status, total_amount, is_read, order_token)
                        VALUES (%s, %s, NOW(), 'Pending', %s, 0, %s)
                        """,
                        (order_id, customer_id, server_total, order_token),
                    )

                for item in items:
                    drug_id = int(item.get("drug_id") or 0)
                    lot_id = int(item.get("lot_id") or 0)
                    qty = int(item.get("quantity") or 0)
                    cur.execute("SELECT price FROM inventory_lots WHERE lot_inventory_id = %s", (lot_id,))
                    row = cur.fetchone()
                    unit_price = float(row["price"] if row else item.get("price_per_unit") or 0)
                    detail_id = next_id(cur, "order_details", "detail_id")
                    cur.execute(
                        """
                        INSERT INTO order_details (detail_id, order_id, drug_id, lot_inventory_id, quantity, price_per_unit)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (detail_id, order_id, drug_id, lot_id, qty, unit_price),
                    )
                    cur.execute(
                        "UPDATE inventory_lots SET current_stock = current_stock - %s WHERE lot_inventory_id = %s",
                        (qty, lot_id),
                    )
                    if drug_id > 0:
                        affected.add(drug_id)

                for drug_id in affected:
                    sync_stock_status_for_drug(cur, drug_id)

                details = f"Online order #{order_id} placed - total ₱{server_total:.2f}."
                if payment_reference:
                    details += f" Paid via {payment_method} ({payment_reference})."
                write_activity_log(cur, "Online Order", details, request=request)

        import secrets
        request.session["order_token"] = secrets.token_hex(32)
        return {
            "success": True,
            "order_id": order_id,
            "total": round(server_total, 2),
            "message": "Order placed successfully.",
            "order_token": request.session["order_token"],
        }
    except PayMongoError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=exc.status_code)
    except ValueError as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=409)
    except Exception as exc:
        return JSONResponse({"success": False, "message": str(exc)}, status_code=409)


@router.get("/prescriptions")
def prescriptions(request: Request):
    customer_id = require_customer(request)
    if customer_id is None:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)
    rows = fetch_all(
        """
        SELECT id, filename, extracted_text, availability_summary, ocr_status, created_at
        FROM prescriptions WHERE user_id = %s ORDER BY created_at DESC
        """,
        (customer_id,),
    )
    out = []
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            for row in rows:
                item = dict(row)
                text = str(item.get("extracted_text") or "").strip()
                if item.get("ocr_status") == "completed" and text:
                    item["availability_summary"] = match_prescription_to_stock(cur, text)
                else:
                    summary = item.get("availability_summary")
                    if isinstance(summary, str) and summary:
                        try:
                            item["availability_summary"] = json.loads(summary)
                        except json.JSONDecodeError:
                            item["availability_summary"] = []
                    elif not summary:
                        item["availability_summary"] = []
                filename = Path(str(item.get("filename") or "")).name
                filepath = ROOT / "uploads" / "prescriptions" / filename
                ext = filepath.suffix.lower().lstrip(".")
                size = filepath.stat().st_size if filepath.is_file() else None
                item["file_type"] = ext.upper() if ext else ""
                item["file_size"] = size
                item["file_available"] = bool(filepath.is_file())
                item["view_url"] = f"/api/customer/prescriptions/{item['id']}/file"
                out.append(item)
    return {"success": True, "prescriptions": out}


_RX_MEDIA = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


@router.get("/prescriptions/{prescription_id}/file")
def prescription_file(request: Request, prescription_id: int):
    customer_id = require_customer(request)
    if customer_id is None:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)
    row = fetch_one(
        "SELECT filename FROM prescriptions WHERE id = %s AND user_id = %s",
        (prescription_id, customer_id),
    )
    if not row or not row.get("filename"):
        return JSONResponse({"success": False, "message": "Prescription not found."}, status_code=404)
    filename = Path(str(row["filename"])).name
    filepath = ROOT / "uploads" / "prescriptions" / filename
    if not filepath.is_file():
        return JSONResponse({"success": False, "message": "File is no longer available."}, status_code=404)
    media = _RX_MEDIA.get(filepath.suffix.lower(), "application/octet-stream")
    return FileResponse(filepath, media_type=media, filename=filename)


def _ocr_image_for_engine(image_path: Path) -> Path:
    """Tesseract/RapidOCR read JPEG/PNG more reliably than WEBP."""
    ext = image_path.suffix.lower()
    if ext in {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff"}:
        return image_path
    try:
        from PIL import Image
        rgb = Image.open(image_path).convert("RGB")
        tmp = Path(tempfile.gettempdir()) / f"pharmalink_ocr_{os.getpid()}_{image_path.stem}.png"
        rgb.save(tmp, "PNG")
        return tmp
    except Exception:
        return image_path


def _run_tesseract(image_path: Path) -> str | None:
    candidates = []
    env_path = os.getenv("TESSERACT_PATH")
    if env_path:
        candidates.append(env_path)
    candidates.extend([
        "tesseract",
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
    ])
    work = _ocr_image_for_engine(image_path)
    for bin_path in candidates:
        try:
            proc = subprocess.run(
                [bin_path, str(work), "stdout", "-l", "eng"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = (proc.stdout or "") + (proc.stderr or "")
            lower = output.lower()
            if any(s in lower for s in ("not recognized", "not found", "no such file", "command not found")):
                continue
            if proc.returncode == 0:
                return (proc.stdout or "").strip()
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return None


_rapid_engine = None


def _run_rapidocr(image_path: Path) -> str | None:
    global _rapid_engine
    try:
        if _rapid_engine is None:
            from rapidocr_onnxruntime import RapidOCR
            _rapid_engine = RapidOCR()
        work = _ocr_image_for_engine(image_path)
        result, _elapse = _rapid_engine(str(work))
    except Exception:
        return None
    if not result:
        return ""
    lines = []
    for item in result:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            text = item[1]
            if text:
                lines.append(str(text).strip())
    return "\n".join(lines).strip()


def _extract_prescription_text(image_path: Path) -> str | None:
    """Return OCR text, empty string if nothing readable, or None if no OCR engine."""
    text = _run_tesseract(image_path)
    if text is not None:
        return text
    return _run_rapidocr(image_path)


@router.post("/prescriptions")
async def upload_prescription(request: Request, prescription_file: UploadFile = File(...)):
    customer_id = require_customer(request)
    if customer_id is None:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)
    if not prescription_file.filename:
        return {"success": False, "message": "Please choose a file to upload."}

    ext = Path(prescription_file.filename).suffix.lower().lstrip(".")
    if ext not in {"jpg", "jpeg", "png", "webp", "pdf"}:
        return {"success": False, "message": "Only JPG, PNG, WEBP, or PDF files are allowed."}
    content = await prescription_file.read()
    if len(content) > 5 * 1024 * 1024:
        return {"success": False, "message": "File must be under 5MB."}

    dest_dir = ROOT / "uploads" / "prescriptions"
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"rx_{customer_id}_{int(time.time())}.{ext}"
    filepath = dest_dir / filename
    filepath.write_bytes(content)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            prescription_id = next_id(cur, "prescriptions", "id")
            cur.execute(
                "INSERT INTO prescriptions (id, user_id, filename, ocr_status, created_at) VALUES (%s, %s, %s, 'pending', NOW())",
                (prescription_id, customer_id, filename),
            )

            extracted_text = None
            availability_summary: list = []
            ocr_status = "failed"
            if ext == "pdf":
                ocr_status = "skipped_pdf"
            else:
                extracted_text = _extract_prescription_text(filepath)
                if extracted_text is None:
                    ocr_status = "unavailable"
                elif extracted_text.strip() == "":
                    ocr_status = "failed"
                else:
                    availability_summary = match_prescription_to_stock(cur, extracted_text)
                    ocr_status = "completed"

            cur.execute(
                "UPDATE prescriptions SET extracted_text = %s, availability_summary = %s, ocr_status = %s WHERE id = %s",
                (extracted_text, json.dumps(availability_summary), ocr_status, prescription_id),
            )
            write_activity_log(
                cur,
                "Upload Prescription",
                f"Uploaded prescription #{prescription_id} ({filename}); OCR {ocr_status}.",
                request=request,
            )

    messages = {
        "unavailable": "Prescription uploaded, but OCR is not installed on this server.",
        "skipped_pdf": "Prescription uploaded. PDF files are stored for pharmacist review (OCR only reads images).",
        "failed": "Prescription uploaded, but the text could not be read clearly. A pharmacist will review it manually.",
        "completed": (
            "Prescription uploaded and read successfully."
            if availability_summary
            else "Prescription uploaded and read, but no matching medicines were found in our catalog."
        ),
    }
    return {
        "success": True,
        "message": messages.get(ocr_status, "Prescription uploaded."),
        "prescription_id": prescription_id,
        "filename": filename,
        "ocr_status": ocr_status,
        "extracted_text": extracted_text,
        "availability_summary": availability_summary,
    }


@router.get("/notifications")
def notifications(request: Request):
    customer_id = require_customer(request)
    if customer_id is None:
        return {"count": 0, "notifications": []}
    rows = fetch_all(
        """
        SELECT order_id, order_status, order_date
        FROM customer_orders
        WHERE customer_id = %s
          AND (order_status = 'Ready for Pickup' OR order_status = 'Completed')
          AND is_read = 0
        ORDER BY order_date DESC
        """,
        (customer_id,),
    )
    notes = [
        {
            "order_id": r["order_id"],
            "message": f"Order #{r['order_id']} status updated to: {r['order_status']}",
            "status": r["order_status"],
            "date": _fmt_dt(r["order_date"], "%b %d, %Y %I:%M %p"),
        }
        for r in rows
    ]
    return {"count": len(notes), "notifications": notes}


@router.post("/notifications/read")
def mark_read(request: Request, order_id: int = 0):
    customer_id = require_customer(request)
    if customer_id is None or order_id <= 0:
        return JSONResponse({"success": False, "message": "Invalid request."}, status_code=400)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE customer_orders SET is_read = 1 WHERE order_id = %s AND customer_id = %s",
                (order_id, customer_id),
            )
    return {"success": True}


@router.get("/products")
def products(request: Request):
    rows = fetch_all(
        """
        SELECT dm.drug_id, dm.category, dm.brand_name, dm.generic_name,
               dm.dosage, dm.form, il.price, il.lot_inventory_id, il.current_stock
        FROM drugs_master dm
        JOIN inventory_lots il ON dm.drug_id = il.drug_id
        WHERE il.current_stock > 0
          AND il.is_active = 1
          AND il.expiration_date >= CURRENT_DATE
          AND dm.is_active = 1
        ORDER BY il.expiration_date ASC
        """
    )
    products = []
    categories = set()
    for row in rows:
        item = dict(row)
        item["category"] = str(item.get("category") or "").title()
        item["generic_name"] = str(item.get("generic_name") or "").title()
        item["brand_name"] = str(item.get("brand_name") or "").title()
        item["form"] = str(item.get("form") or "").title()
        categories.add(item["category"])
        products.append(item)
    return {"success": True, "products": products, "categories": sorted(c for c in categories if c)}
