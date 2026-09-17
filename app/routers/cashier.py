import json
import hashlib
import random
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from psycopg2.extras import RealDictCursor

from app.activity import log_event, write_activity_log
from app.db import fetch_all, fetch_one, get_conn, next_id
from app.deps import require_admin, require_staff
from app.payments import CASHIER_PAYMENT_KEYS
from app.stock import sync_stock_status_for_drug
from app.validation import validate_profile_fields

router = APIRouter(prefix="/api/cashier", tags=["cashier"])
ROOT = Path(__file__).resolve().parent.parent.parent
POINTS_TO_PESO_RATE = 0.30
DEFAULT_AVATAR = "https://cdn-icons-png.flaticon.com/512/2922/2922510.png"


def _unauthorized():
    return JSONResponse({"success": False, "message": "Not authorized."}, status_code=401)


def _title(value):
    return str(value or "").title()


def _fmt_dt(value, fmt: str) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    return value.strftime(fmt)


@router.get("/products")
def products(request: Request):
    if not require_staff(request):
        return _unauthorized()
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
    for row in rows:
        item = dict(row)
        item["category"] = _title(item.get("category"))
        item["generic_name"] = _title(item.get("generic_name"))
        item["brand_name"] = _title(item.get("brand_name"))
        item["form"] = _title(item.get("form"))
        products.append(item)
    return products


@router.get("/products/barcode")
def product_by_barcode(request: Request, barcode: str = ""):
    if not require_staff(request):
        return _unauthorized()
    barcode = barcode.strip()
    if not barcode:
        return JSONResponse({"success": False, "message": "No barcode provided."}, status_code=400)
    drug = fetch_one(
        """
        SELECT dm.drug_id, dm.category, dm.brand_name, dm.generic_name,
               dm.dosage, dm.form, dm.barcode
        FROM drugs_master dm
        WHERE dm.is_active = 1
          AND btrim(dm.barcode) = btrim(%s)
        """,
        (barcode,),
    )
    if not drug:
        return {"success": False, "message": f'No product found for barcode "{barcode}".'}
    row = fetch_one(
        """
        SELECT price, lot_inventory_id, current_stock, expiration_date
        FROM inventory_lots
        WHERE drug_id = %s
          AND is_active = 1
          AND expiration_date >= CURRENT_DATE
        ORDER BY (current_stock > 0) DESC, expiration_date ASC
        LIMIT 1
        """,
        (drug["drug_id"],),
    )
    if not row:
        return {
            "success": False,
            "message": "Barcode matched, but this drug has no sellable stock lot yet. Add or receive stock first.",
            "generic_name": drug["generic_name"],
            "brand_name": drug["brand_name"],
        }
    if int(row["current_stock"] or 0) <= 0:
        return {
            "success": False,
            "message": "This product is currently out of stock.",
            "generic_name": drug["generic_name"],
            "brand_name": drug["brand_name"],
        }
    product = {**dict(drug), **dict(row)}
    product["category"] = _title(product.get("category"))
    product["generic_name"] = _title(product.get("generic_name"))
    product["brand_name"] = _title(product.get("brand_name"))
    product["form"] = _title(product.get("form"))
    return {"success": True, "product": product}


@router.get("/categories")
def categories(request: Request):
    if not require_staff(request):
        return _unauthorized()
    rows = fetch_all("SELECT DISTINCT category FROM drugs_master ORDER BY category ASC")
    out = []
    for row in rows:
        cat = _title(row.get("category"))
        if cat:
            out.append(cat)
    return out


@router.get("/customers")
def customers(request: Request):
    if not require_staff(request):
        return _unauthorized()
    rows = fetch_all(
        """
        SELECT customer_id,
               CONCAT_WS(' ', first_name, middle_name, last_name) AS name,
               COALESCE(loyalty_points, 0) AS loyalty_points
        FROM customers
        WHERE is_active = 1
        ORDER BY last_name ASC
        """
    )
    return [{"customer_id": r["customer_id"], "name": r["name"], "loyalty_points": float(r["loyalty_points"] or 0)} for r in rows]


@router.get("/promos")
def promos(request: Request):
    if not require_staff(request):
        return _unauthorized()
    rows = fetch_all(
        """
        SELECT promo_id, name, discount_type, discount_value, drug_id, category
        FROM promos
        WHERE is_active = 1
          AND CURRENT_DATE BETWEEN start_date AND end_date
        """
    )
    out = []
    promo_ids = []
    for row in rows:
        item = dict(row)
        item["discount_value"] = float(item["discount_value"] or 0)
        item["drug_id"] = int(item["drug_id"]) if item.get("drug_id") is not None else None
        item["drug_ids"] = []
        out.append(item)
        promo_ids.append(int(item["promo_id"]))
    if promo_ids:
        links = fetch_all(
            "SELECT promo_id, drug_id FROM promo_drugs WHERE promo_id = ANY(%s)",
            (promo_ids,),
        )
        by_promo = {}
        for link in links:
            by_promo.setdefault(int(link["promo_id"]), []).append(int(link["drug_id"]))
        for item in out:
            item["drug_ids"] = by_promo.get(int(item["promo_id"]), [])
    return out


@router.get("/online-orders")
def online_orders(request: Request):
    if not require_staff(request):
        return _unauthorized()
    rows = fetch_all(
        """
        SELECT co.order_id, co.order_date, co.order_status, c.first_name, c.last_name
        FROM customer_orders co
        JOIN customers c ON co.customer_id = c.customer_id
        WHERE co.order_status IN ('Pending', 'Processing', 'Ready for Pickup')
        ORDER BY co.order_date ASC
        """
    )
    orders = []
    for row in rows:
        status = row["order_status"]
        orders.append({
            "order_id": row["order_id"],
            "display_id": f"ORD-{row['order_id']}",
            "customer_name": f"{row['first_name']} {row['last_name']}",
            "status": status,
            "status_class": str(status).lower().replace(" ", "-"),
            "date": _fmt_dt(row["order_date"], "%b %d, %Y %I:%M %p"),
        })
    return {"success": True, "orders": orders}


@router.get("/online-orders/{order_id}")
def online_order_details(order_id: int, request: Request):
    if not require_staff(request):
        return _unauthorized()
    header = fetch_one("SELECT customer_id, order_status FROM customer_orders WHERE order_id = %s", (order_id,))
    if not header:
        return JSONResponse({"success": False, "message": "Order not found."}, status_code=404)
    customer = fetch_one(
        "SELECT first_name, middle_name, last_name, loyalty_points FROM customers WHERE customer_id = %s",
        (header["customer_id"],),
    )
    parts = []
    if customer:
        parts = [str(customer.get(k) or "").strip() for k in ("first_name", "middle_name", "last_name")]
    items = fetch_all(
        """
        SELECT od.drug_id, od.lot_inventory_id, od.quantity AS ordered_qty, od.price_per_unit,
               dm.brand_name, dm.generic_name, il.current_stock
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
        "customer_name": " ".join(p for p in parts if p) or "Customer N/A",
        "customer_id": header["customer_id"],
        "loyalty_points": float(customer["loyalty_points"]) if customer else 0,
        "status": header["order_status"],
        "items": [dict(i) for i in items],
    }


@router.post("/online-orders/status")
async def update_order_status(request: Request):
    if not require_staff(request):
        return _unauthorized()
    payload = await request.json()
    order_id = int(payload.get("order_id") or 0)
    status = str(payload.get("status") or "").strip()
    allowed = {"Pending", "Processing", "Ready for Pickup", "Completed", "Cancelled"}
    if order_id <= 0 or status not in allowed:
        return {"success": False, "message": "Invalid order_id or status."}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE customer_orders SET order_status = %s WHERE order_id = %s", (status, order_id))
            write_activity_log(
                cur,
                "Update Order Status",
                f"Order #{order_id} status set to '{status}'.",
                request=request,
            )
    return {"success": True}


@router.get("/transactions")
def transactions(request: Request):
    if not require_staff(request):
        return _unauthorized()
    rows = fetch_all(
        """
        SELECT sale_id AS transaction_id, total_amount, cash_received, change_amount, date_created
        FROM sales
        WHERE status = 'completed'
        ORDER BY date_created DESC
        LIMIT 10
        """
    )
    return [dict(r) for r in rows]


@router.get("/daily-sales")
def daily_sales(request: Request):
    if not require_staff(request):
        return _unauthorized()
    labels = []
    data = []
    today = datetime.now().date()
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        labels.append(day.strftime("%a"))
        row = fetch_one(
            "SELECT COALESCE(SUM(total_amount), 0) AS total FROM sales WHERE date_created::date = %s AND status = 'completed'",
            (day.isoformat(),),
        )
        data.append(round(float(row["total"] or 0), 2))
    return {"labels": labels, "data": data}


@router.get("/top-selling")
def top_selling(request: Request, startDate: str = "", endDate: str = ""):
    if not require_staff(request):
        return _unauthorized()
    start = startDate or datetime.now().strftime("%Y-%m-%d")
    end = endDate or datetime.now().strftime("%Y-%m-%d")
    rows = fetch_all(
        """
        SELECT d.generic_name, d.brand_name, COALESCE(SUM(si.quantity), 0) AS total_quantity_sold
        FROM sales_items si
        JOIN sales s ON si.sale_id = s.sale_id
        JOIN drugs_master d ON si.drug_id = d.drug_id
        WHERE s.date_created::date BETWEEN %s AND %s
          AND s.status = 'completed'
        GROUP BY d.drug_id, d.generic_name, d.brand_name
        ORDER BY total_quantity_sold DESC
        LIMIT 8
        """,
        (start, end),
    )
    return [
        {
            "generic_name": r["generic_name"],
            "brand_name": r["brand_name"],
            "total_quantity_sold": int(r["total_quantity_sold"] or 0),
        }
        for r in rows
    ]


@router.get("/recent-transactions")
def recent_transactions(request: Request, startDate: str = "", endDate: str = ""):
    if not require_staff(request):
        return _unauthorized()
    start = startDate or (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    end = endDate or datetime.now().strftime("%Y-%m-%d")
    rows = fetch_all(
        """
        SELECT s.date_created,
               COALESCE(CONCAT(c.first_name, ' ', c.last_name), 'Guest') AS customer_name,
               s.total_amount, s.status
        FROM sales s
        LEFT JOIN customers c ON s.customer_id = c.customer_id
        WHERE s.date_created::date BETWEEN %s AND %s
        ORDER BY s.date_created DESC
        LIMIT 5
        """,
        (start, end),
    )
    out = []
    for row in rows:
        item = dict(row)
        item["time_display"] = _fmt_dt(row["date_created"], "%H:%M")
        item["total_amount"] = f"{float(row['total_amount'] or 0):.2f}"
        out.append(item)
    return out


@router.get("/sales-report")
def sales_report(request: Request, startDate: str = "", endDate: str = "", q: str = "", payment: str = ""):
    if not require_staff(request):
        return _unauthorized()
    start = startDate or (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    end = endDate or datetime.now().strftime("%Y-%m-%d")
    needle = (q or "").strip().lower()
    pay = (payment or "").strip().lower()
    params: list = [start, end]
    extra_sql = ""
    if pay in CASHIER_PAYMENT_KEYS:
        extra_sql += " AND LOWER(COALESCE(s.payment_method, '')) = %s"
        params.append(pay)
    like = f"%{needle}%"
    search_with_ref = """
          AND (
            LOWER(COALESCE(s.transaction_id, '')) LIKE %s
            OR LOWER(COALESCE(s.payment_reference, '')) LIKE %s
            OR LOWER(COALESCE(c.first_name, '') || ' ' || COALESCE(c.last_name, '')) LIKE %s
            OR LOWER(COALESCE(st.first_name, '') || ' ' || COALESCE(st.last_name, '')) LIKE %s
          )
    """
    search_no_ref = """
          AND (
            LOWER(COALESCE(s.transaction_id, '')) LIKE %s
            OR LOWER(COALESCE(c.first_name, '') || ' ' || COALESCE(c.last_name, '')) LIKE %s
            OR LOWER(COALESCE(st.first_name, '') || ' ' || COALESCE(st.last_name, '')) LIKE %s
          )
    """
    base_from = """
        FROM sales s
        LEFT JOIN customers c ON s.customer_id = c.customer_id
        LEFT JOIN staff_info st ON st.user_id = s.user_id
        WHERE s.date_created::date BETWEEN %s AND %s
          AND s.status = 'completed'
    """
    select_sql = """
        SELECT s.sale_id, s.transaction_id, s.date_created, s.total_amount, s.subtotal,
               s.discount_amount, s.tax_amount, s.cash_received, s.change_amount,
               s.payment_method, s.payment_reference, s.status,
               COALESCE(NULLIF(TRIM(CONCAT(c.first_name, ' ', c.last_name)), ''), 'Guest') AS customer_name,
               COALESCE(NULLIF(TRIM(CONCAT(st.first_name, ' ', st.last_name)), ''), 'Staff') AS cashier_name
    """
    query_params = list(params)
    query_sql = select_sql + base_from + extra_sql
    if needle:
        query_sql += search_with_ref
        query_params.extend([like, like, like, like])
    query_sql += " ORDER BY s.date_created DESC LIMIT 250"
    try:
        rows = fetch_all(query_sql, tuple(query_params))
    except Exception:
        query_params = list(params)
        query_sql = select_sql.replace("s.payment_reference, ", "") + base_from + extra_sql
        if needle:
            query_sql += search_no_ref
            query_params.extend([like, like, like])
        query_sql += " ORDER BY s.date_created DESC LIMIT 250"
        rows = fetch_all(query_sql, tuple(query_params))
    logs = []
    total_sales = 0.0
    cash_count = gcash_count = maya_count = 0
    for row in rows:
        amount = float(row.get("total_amount") or 0)
        total_sales += amount
        method = str(row.get("payment_method") or "cash").lower()
        if method == "gcash":
            gcash_count += 1
        elif method == "maya":
            maya_count += 1
        else:
            cash_count += 1
        logs.append({
            "sale_id": row["sale_id"],
            "transaction_id": row.get("transaction_id") or f"SALE-{row['sale_id']}",
            "date": _fmt_dt(row.get("date_created"), "%b %d, %Y"),
            "time": _fmt_dt(row.get("date_created"), "%I:%M %p"),
            "customer_name": row.get("customer_name") or "Guest",
            "cashier_name": row.get("cashier_name") or "Staff",
            "payment_method": method,
            "payment_reference": row.get("payment_reference") or "",
            "total_amount": round(amount, 2),
            "cash_received": round(float(row.get("cash_received") or 0), 2),
            "change_amount": round(float(row.get("change_amount") or 0), 2),
            "status": row.get("status") or "completed",
        })
    items_row = fetch_one(
        """
        SELECT COALESCE(SUM(si.quantity), 0) AS items_sold
        FROM sales_items si
        JOIN sales s ON si.sale_id = s.sale_id
        WHERE s.date_created::date BETWEEN %s AND %s
          AND s.status = 'completed'
        """,
        (start, end),
    )
    return {
        "success": True,
        "start": start,
        "end": end,
        "summary": {
            "transaction_count": len(logs),
            "total_sales": round(total_sales, 2),
            "items_sold": int((items_row or {}).get("items_sold") or 0),
            "cash_count": cash_count,
            "gcash_count": gcash_count,
            "maya_count": maya_count,
        },
        "logs": logs,
    }


@router.get("/sales-report/{sale_id}")
def sales_report_detail(request: Request, sale_id: int):
    if not require_staff(request):
        return _unauthorized()
    sale = fetch_one(
        """
        SELECT s.sale_id, s.transaction_id, s.date_created, s.total_amount, s.subtotal,
               s.discount_amount, s.tax_amount, s.cash_received, s.change_amount,
               s.payment_method, s.status,
               COALESCE(NULLIF(TRIM(CONCAT(c.first_name, ' ', c.last_name)), ''), 'Guest') AS customer_name,
               COALESCE(NULLIF(TRIM(CONCAT(st.first_name, ' ', st.last_name)), ''), 'Staff') AS cashier_name
        FROM sales s
        LEFT JOIN customers c ON s.customer_id = c.customer_id
        LEFT JOIN staff_info st ON st.user_id = s.user_id
        WHERE s.sale_id = %s
        """,
        (sale_id,),
    )
    if not sale:
        return JSONResponse({"success": False, "message": "Transaction not found."}, status_code=404)
    ref = ""
    try:
        ref_row = fetch_one("SELECT payment_reference FROM sales WHERE sale_id = %s", (sale_id,))
        ref = (ref_row or {}).get("payment_reference") or ""
    except Exception:
        ref = ""
    items = fetch_all(
        """
        SELECT si.quantity, si.price, si.subtotal, si.promo_name,
               d.generic_name, d.brand_name
        FROM sales_items si
        JOIN drugs_master d ON d.drug_id = si.drug_id
        WHERE si.sale_id = %s
        ORDER BY si.id
        """,
        (sale_id,),
    )
    return {
        "success": True,
        "sale": {
            "sale_id": sale["sale_id"],
            "transaction_id": sale.get("transaction_id") or f"SALE-{sale['sale_id']}",
            "date": _fmt_dt(sale.get("date_created"), "%b %d, %Y %I:%M %p"),
            "customer_name": sale.get("customer_name") or "Guest",
            "cashier_name": sale.get("cashier_name") or "Staff",
            "payment_method": str(sale.get("payment_method") or "cash").lower(),
            "payment_reference": ref,
            "subtotal": round(float(sale.get("subtotal") or 0), 2),
            "discount_amount": round(float(sale.get("discount_amount") or 0), 2),
            "tax_amount": round(float(sale.get("tax_amount") or 0), 2),
            "total_amount": round(float(sale.get("total_amount") or 0), 2),
            "cash_received": round(float(sale.get("cash_received") or 0), 2),
            "change_amount": round(float(sale.get("change_amount") or 0), 2),
            "status": sale.get("status") or "completed",
        },
        "items": [
            {
                "name": f"{r.get('generic_name') or ''} ({r.get('brand_name') or ''})".strip(),
                "quantity": int(r.get("quantity") or 0),
                "price": round(float(r.get("price") or 0), 2),
                "subtotal": round(float(r.get("subtotal") or 0) or (float(r.get("price") or 0) * int(r.get("quantity") or 0)), 2),
                "promo_name": r.get("promo_name") or "",
            }
            for r in items
        ],
    }


@router.get("/dashboard")
def dashboard(request: Request, startDate: str = "", endDate: str = ""):
    if not require_staff(request):
        return _unauthorized()
    start = startDate or datetime.now().strftime("%Y-%m-%d")
    end = endDate or datetime.now().strftime("%Y-%m-%d")
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE drugs_master dm
                SET stock_status = CASE
                    WHEN COALESCE((SELECT SUM(current_stock) FROM inventory_lots WHERE is_active = 1 AND drug_id = dm.drug_id), 0) <= 0 THEN 'out'
                    WHEN COALESCE((SELECT SUM(current_stock) FROM inventory_lots WHERE is_active = 1 AND drug_id = dm.drug_id), 0) <= dm.minimum_stock THEN 'low'
                    ELSE 'ok'
                END
                WHERE dm.is_active = 1
                """
            )
            cur.execute(
                "SELECT COUNT(sale_id) AS transaction_count FROM sales WHERE date_created::date BETWEEN %s AND %s AND status = 'completed'",
                (start, end),
            )
            transaction_count = int(cur.fetchone()["transaction_count"] or 0)
            cur.execute(
                "SELECT COALESCE(SUM(total_amount), 0) AS total_sales FROM sales WHERE date_created::date BETWEEN %s AND %s AND status = 'completed'",
                (start, end),
            )
            total_sales = float(cur.fetchone()["total_sales"] or 0)
            cur.execute(
                """
                SELECT COALESCE(SUM(si.quantity), 0) AS items_sold
                FROM sales_items si JOIN sales s ON si.sale_id = s.sale_id
                WHERE s.date_created::date BETWEEN %s AND %s AND s.status = 'completed'
                """,
                (start, end),
            )
            items_sold = int(cur.fetchone()["items_sold"] or 0)
            cur.execute("SELECT COUNT(*) AS cnt FROM drugs_master WHERE is_active = 1 AND stock_status = 'low'")
            low_stock_count = int(cur.fetchone()["cnt"] or 0)
            cur.execute("SELECT COUNT(*) AS cnt FROM drugs_master WHERE is_active = 1 AND stock_status = 'out'")
            out_of_stock_count = int(cur.fetchone()["cnt"] or 0)
            cur.execute(
                "SELECT COUNT(order_id) AS pending_orders FROM customer_orders WHERE order_status IN ('Pending', 'Processing')"
            )
            pending_orders = int(cur.fetchone()["pending_orders"] or 0)
    return {
        "transaction_count": transaction_count,
        "total_sales": f"{total_sales:.2f}",
        "items_sold": items_sold,
        "low_stock_count": low_stock_count,
        "out_of_stock_count": out_of_stock_count,
        "pending_orders": pending_orders,
    }


@router.get("/charts")
def charts(request: Request):
    if not require_staff(request):
        return _unauthorized()
    freq_rows = fetch_all(
        """
        SELECT CONCAT(c.first_name, ' ', c.last_name) AS fullname, COUNT(o.order_id) AS order_count
        FROM customers c
        LEFT JOIN customer_orders o ON c.customer_id = o.customer_id
        GROUP BY c.customer_id, c.first_name, c.last_name
        ORDER BY order_count DESC
        """
    )
    spend_rows = fetch_all(
        """
        SELECT c.customer_id, CONCAT(c.first_name, ' ', c.last_name) AS customer_name,
               c.loyalty_points, SUM(co.total_amount) AS total_spent,
               COUNT(co.order_id) AS order_count, MAX(co.order_date) AS last_order_date
        FROM customers c
        LEFT JOIN customer_orders co ON c.customer_id = co.customer_id
        GROUP BY c.customer_id, c.first_name, c.last_name, c.loyalty_points
        ORDER BY total_spent DESC
        """
    )
    type_rows = fetch_all(
        "SELECT customer_type, COUNT(*) as total FROM customers WHERE is_active = 1 GROUP BY customer_type"
    )
    return {
        "freq_labels": [r["fullname"] for r in freq_rows],
        "freq_data": [int(r["order_count"] or 0) for r in freq_rows],
        "spend_labels": [r["customer_name"] for r in spend_rows],
        "spend_data": [float(r["total_spent"] or 0) for r in spend_rows],
        "type_labels": [r["customer_type"] for r in type_rows],
        "type_data": [int(r["total"] or 0) for r in type_rows],
    }


@router.post("/sales")
async def create_sale(request: Request):
    user_id = require_staff(request)
    if not user_id:
        return JSONResponse({"status": "error", "message": "Not authorized. Please log in again."}, status_code=401)
    payload = await request.json()
    items = payload.get("items") or []
    if not items:
        return JSONResponse({"status": "error", "message": "Cart is empty."}, status_code=400)

    customer_id = payload.get("customer_id")
    try:
        customer_id = int(customer_id) if customer_id not in (None, "") else None
    except (TypeError, ValueError):
        customer_id = None
    if customer_id is not None and customer_id <= 0:
        customer_id = None
    subtotal = float(payload.get("subtotal") or 0)
    discount_total = float(payload.get("discount_total") or 0)
    tax_total = float(payload.get("tax_total") or 0)
    total_amount = float(payload.get("total_amount") or 0)
    cash_received = float(payload.get("cash_received") or 0)
    change_amount = float(payload.get("change_amount") or 0)
    payment_method = payload.get("payment_method") or "cash"
    payment_reference = str(payload.get("payment_reference") or "").strip() or None
    points_requested = float(payload.get("points_redeemed") or 0)

    if payment_method not in CASHIER_PAYMENT_KEYS:
        return JSONResponse({"status": "error", "message": "Invalid payment method."}, status_code=400)
    if total_amount <= 0:
        return JSONResponse({"status": "error", "message": "Invalid transaction total."}, status_code=400)
    if cash_received < total_amount:
        return JSONResponse({"status": "error", "message": "Insufficient cash received."}, status_code=400)

    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                affected = set()
                for item in items:
                    lot_id = int(item.get("lot_id") or 0)
                    qty = int(item.get("qty") or 0)
                    if lot_id <= 0 or qty <= 0:
                        raise ValueError(f"Invalid item in cart (lot #{lot_id}, qty {qty}).")
                    cur.execute(
                        "SELECT current_stock FROM inventory_lots WHERE lot_inventory_id = %s AND is_active = 1 FOR UPDATE",
                        (lot_id,),
                    )
                    lot = cur.fetchone()
                    if not lot:
                        raise ValueError(f"Item (lot #{lot_id}) no longer exists or is inactive.")
                    if int(lot["current_stock"]) < qty:
                        raise ValueError(
                            f"Not enough stock for lot #{lot_id} — only {lot['current_stock']} left, but {qty} requested. Please refresh and try again."
                        )

                points_redeemed = 0.0
                points_discount_value = 0.0
                new_loyalty_balance = None
                if points_requested > 0:
                    if customer_id is None:
                        raise ValueError("Points can only be redeemed for a registered customer — this sale has no customer selected.")
                    cur.execute(
                        "SELECT loyalty_points FROM customers WHERE customer_id = %s AND is_active = 1 FOR UPDATE",
                        (customer_id,),
                    )
                    cust = cur.fetchone()
                    if not cust:
                        raise ValueError("Selected customer no longer exists or is inactive.")
                    available = float(cust["loyalty_points"] or 0)
                    if points_requested > available:
                        if points_requested - available <= 1.0:
                            points_requested = available
                        else:
                            raise ValueError(f"Not enough loyalty points — only {available:.2f} available.")
                    points_discount_value = round(points_requested * POINTS_TO_PESO_RATE, 2)
                    if points_discount_value > subtotal:
                        points_discount_value = subtotal
                        points_redeemed = round(points_discount_value / POINTS_TO_PESO_RATE, 2) if points_discount_value else 0
                    else:
                        points_redeemed = points_requested

                transaction_id = f"TXN-{datetime.now().strftime('%Y%m%d%H%M%S')}-{random.randint(1000, 9999)}"
                discount_amount_with_points = discount_total + points_discount_value
                sale_id = next_id(cur, "sales", "sale_id")
                sale_params = (
                    sale_id, transaction_id, customer_id, user_id, subtotal, discount_amount_with_points,
                    tax_total, total_amount, cash_received, change_amount, payment_method, payment_reference,
                    points_redeemed, points_discount_value,
                )
                cur.execute("SAVEPOINT sale_header")
                try:
                    cur.execute(
                        """
                        INSERT INTO sales
                            (sale_id, transaction_id, customer_id, user_id, subtotal, discount_amount, tax_amount,
                             total_amount, cash_received, change_amount, payment_method, payment_reference,
                             points_redeemed, points_discount_value, status, date_created)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'completed', NOW())
                        """,
                        sale_params,
                    )
                except Exception:
                    cur.execute("ROLLBACK TO SAVEPOINT sale_header")
                    cur.execute(
                        """
                        INSERT INTO sales
                            (sale_id, transaction_id, customer_id, user_id, subtotal, discount_amount, tax_amount,
                             total_amount, cash_received, change_amount, payment_method,
                             points_redeemed, points_discount_value, status, date_created)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'completed', NOW())
                        """,
                        (
                            sale_id, transaction_id, customer_id, user_id, subtotal, discount_amount_with_points,
                            tax_total, total_amount, cash_received, change_amount, payment_method,
                            points_redeemed, points_discount_value,
                        ),
                    )
                for item in items:
                    drug_id = int(item.get("drug_id") or 0)
                    lot_id = int(item.get("lot_id") or 0)
                    qty = int(item.get("qty") or 0)
                    price = float(item.get("price") or 0)
                    line_subtotal = float(item.get("subtotal") or (price * qty))
                    discount_amount = float(item.get("discount_amount") or 0)
                    promo_name = item.get("promo_name")
                    vat_exempt = int(item.get("vat_exempt") or 0)
                    item_id = next_id(cur, "sales_items", "id")
                    cur.execute(
                        """
                        INSERT INTO sales_items
                            (id, sale_id, drug_id, lot_id, quantity, price, subtotal, discount_amount, promo_name, vat_exempt)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (item_id, sale_id, drug_id, lot_id, qty, price, line_subtotal, discount_amount, promo_name, vat_exempt),
                    )
                    cur.execute(
                        "UPDATE inventory_lots SET current_stock = current_stock - %s WHERE lot_inventory_id = %s",
                        (qty, lot_id),
                    )
                    if drug_id > 0:
                        affected.add(drug_id)

                if points_redeemed > 0:
                    cur.execute(
                        "UPDATE customers SET loyalty_points = loyalty_points - %s WHERE customer_id = %s",
                        (points_redeemed, customer_id),
                    )
                    cur.execute("SELECT loyalty_points FROM customers WHERE customer_id = %s", (customer_id,))
                    bal = cur.fetchone()
                    new_loyalty_balance = float(bal["loyalty_points"]) if bal else None

                for drug_id in affected:
                    sync_stock_status_for_drug(cur, drug_id)

                details = f"Sale #{sale_id} — total ₱{total_amount:.2f}, {len(items)} item(s)."
                if points_redeemed > 0:
                    details += f" Points redeemed: {points_redeemed:.2f} (₱{points_discount_value:.2f})."
                write_activity_log(cur, "POS Sale", details, request=request)

        return {
            "status": "success",
            "sale_id": sale_id,
            "transaction_id": transaction_id,
            "points_redeemed": points_redeemed,
            "points_discount_value": points_discount_value,
            "new_loyalty_balance": new_loyalty_balance,
            "message": "Transaction saved successfully.",
        }
    except ValueError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=409)
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=409)


@router.post("/loyalty-points")
async def loyalty_points(request: Request):
    if not require_admin(request):
        return _unauthorized()
    payload = await request.json()
    customer_id = payload.get("customer_id")
    points = float(payload.get("points") or 0)
    if customer_id is None or not str(customer_id).replace("-", "").isdigit() or points == 0:
        return {"success": False, "message": "Please provide a customer and a non-zero points value."}
    customer_id = int(customer_id)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT loyalty_points FROM customers WHERE customer_id = %s FOR UPDATE", (customer_id,))
            row = cur.fetchone()
            if not row:
                return {"success": False, "message": "Customer not found."}
            new_balance = max(0.0, float(row["loyalty_points"] or 0) + points)
            cur.execute("UPDATE customers SET loyalty_points = %s WHERE customer_id = %s", (new_balance, customer_id))
            details = f"Customer ID {customer_id} points {points:+} (new balance: {new_balance}) — via Customer Segmentation."
            write_activity_log(cur, "Adjust Loyalty Points", details, request=request)
    return {"success": True, "new_balance": new_balance}


@router.get("/profile")
def get_profile(request: Request):
    user_id = require_staff(request)
    if not user_id:
        return _unauthorized()
    staff = fetch_one(
        """
        SELECT si.first_name, si.middle_name, si.last_name, si.email, si.phone_number, si.address, si.profile_image,
               u.username
        FROM staff_info si
        JOIN users u ON si.user_id = u.user_id
        WHERE si.user_id = %s
        """,
        (user_id,),
    ) or {}
    staff = dict(staff) if staff else {}
    staff["email"] = staff.get("email") or staff.get("username") or ""
    staff["profile_image"] = staff.get("profile_image") or DEFAULT_AVATAR
    staff["first_name"] = staff.get("first_name") or request.session.get("user_first_name") or "Cashier"
    return {"success": True, "staff": staff}


@router.post("/profile")
async def update_profile(request: Request):
    user_id = require_staff(request)
    if not user_id:
        return _unauthorized()
    data = await request.json()
    first_name = str(data.get("first_name") or "").strip()
    middle_name = str(data.get("middle_name") or "").strip()
    last_name = str(data.get("last_name") or "").strip()
    email = str(data.get("email") or "").strip()
    phone_number = str(data.get("phone_number") or "").strip()
    address = str(data.get("address") or "").strip()
    if not first_name or not last_name:
        return {"success": False, "message": "First and last name are required."}
    err = validate_profile_fields(first_name, last_name, email, phone_number, address, middle_name)
    if err:
        return {"success": False, "message": err}
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            staff_id = next_id(cur, "staff_info", "staff_id")
            cur.execute(
                """
                INSERT INTO staff_info (staff_id, user_id, first_name, middle_name, last_name, email, phone_number, address)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    first_name = EXCLUDED.first_name,
                    middle_name = EXCLUDED.middle_name,
                    last_name = EXCLUDED.last_name,
                    email = EXCLUDED.email,
                    phone_number = EXCLUDED.phone_number,
                    address = EXCLUDED.address
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
                f"Updated cashier profile ({first_name} {last_name}).",
                actor=f"{first_name} {last_name}".strip(),
            )
    if not saved:
        return {"success": False, "message": "Save did not persist correctly. Please try again or contact support."}
    request.session["user_first_name"] = saved["first_name"]
    request.session["user_last_name"] = saved.get("last_name") or ""
    return {"success": True, "message": "Profile updated successfully.", **dict(saved)}


@router.get("/segmentation")
def segmentation(request: Request):
    if not require_staff(request):
        return _unauthorized()
    spend_rows = fetch_all(
        """
        SELECT c.customer_id, CONCAT(c.first_name, ' ', c.last_name) AS customer_name,
               c.loyalty_points, SUM(co.total_amount) AS total_spent,
               COUNT(co.order_id) AS order_count, MAX(co.order_date) AS last_order_date
        FROM customers c
        LEFT JOIN customer_orders co ON c.customer_id = co.customer_id
        GROUP BY c.customer_id, c.first_name, c.last_name, c.loyalty_points
        ORDER BY total_spent DESC
        """
    )
    spend_customers = []
    spend_data = []
    now = datetime.now()
    for row in spend_rows:
        last = row["last_order_date"]
        if last and not isinstance(last, datetime):
            try:
                last = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
            except ValueError:
                last = None
        recency = int((now - last.replace(tzinfo=None)).days) if last else 9999
        amt = float(row["total_spent"] or 0)
        spend_data.append(amt)
        spend_customers.append({
            "customer_id": int(row["customer_id"]),
            "name": row["customer_name"],
            "loyalty_points": float(row["loyalty_points"] or 0),
            "frequency": int(row["order_count"] or 0),
            "recency_days": recency,
            "total_spent": amt,
        })

    fingerprint = hashlib.md5(json.dumps(spend_customers, default=str).encode()).hexdigest()
    cache_file = ROOT / "cache" / "segment_kmeans_cache.json"
    stored_k = 3
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            stored_k = int(cached.get("active_k") or (cached.get("payload") or {}).get("active_k") or 3)
            if stored_k not in (2, 3, 4, 5):
                stored_k = 3
            if cached.get("fingerprint") == fingerprint and cached.get("payload"):
                payload = cached["payload"]
                payload["active_k"] = stored_k
                return payload
        except Exception:
            stored_k = 3

    label_sets = {
        2: ["High Spend", "Low Spend"],
        3: ["Loyal Customers", "Occasional Buyers", "Low Engagement"],
        4: ["VIP", "Regular", "Occasional", "Inactive"],
        5: ["VIP", "High", "Medium", "Low", "Inactive"],
    }
    kmeans_results = _run_kmeans(spend_customers)
    variants = {}
    engine = "quantile"
    silhouette = {}
    debug = {}
    total = len(spend_customers)
    for n, labels in label_sets.items():
        counts = [0] * n
        sums = [0.0] * n
        members = [[] for _ in range(n)]
        used = False
        result = (kmeans_results or {}).get(str(n))
        if result and result.get("success"):
            rank_of = {cid: i for i, cid in enumerate(result.get("cluster_order") or [])}
            assignments = result.get("assignments") or {}
            for idx, cust in enumerate(spend_customers):
                raw = assignments.get(str(cust["customer_id"]))
                if raw is None:
                    continue
                rank = rank_of.get(raw, n - 1)
                amt = spend_data[idx]
                counts[rank] += 1
                sums[rank] += amt
                members[rank].append({
                    "customer_id": cust["customer_id"],
                    "name": cust["name"],
                    "total_spent": round(amt, 2),
                    "loyalty_points": cust["loyalty_points"],
                })
            used = True
            engine = "kmeans"
            if result.get("silhouette_score") is not None:
                silhouette[n] = result["silhouette_score"]
        if not used and total > 0:
            for idx, amt in enumerate(spend_data):
                bucket = min(n - 1, int((idx / total) * n))
                counts[bucket] += 1
                sums[bucket] += amt
                cust = spend_customers[idx]
                members[bucket].append({
                    "customer_id": cust["customer_id"],
                    "name": cust["name"],
                    "total_spent": round(amt, 2),
                    "loyalty_points": cust["loyalty_points"],
                })
        avgs = [round(sums[i] / counts[i], 2) if counts[i] else 0 for i in range(n)]
        variants[str(n)] = {"labels": labels, "counts": counts, "avgSpend": avgs, "members": members}

    payload = {
        "variants": variants,
        "engine": engine,
        "silhouette": silhouette,
        "debug": debug,
        "active_k": stored_k,
    }
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"fingerprint": fingerprint, "active_k": stored_k, "payload": payload}, default=str),
            encoding="utf-8",
        )
    except Exception:
        pass
    return payload


@router.post("/segmentation/config")
async def segmentation_config(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    try:
        k = int(data.get("k") or 0)
    except (TypeError, ValueError):
        k = 0
    if k not in (2, 3, 4, 5):
        return {"success": False, "message": "Choose 2 to 5 segments."}
    cache_file = ROOT / "cache" / "segment_kmeans_cache.json"
    cached = {}
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
        except Exception:
            cached = {}
    cached["active_k"] = k
    payload = cached.get("payload") or {}
    payload["active_k"] = k
    cached["payload"] = payload
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cached, default=str), encoding="utf-8")
    log_event("Update Segmentation", f"Set customer segments to {k}.", request=request)
    return {"success": True, "active_k": k}


def _run_kmeans(spend_customers):
    script = ROOT / "python" / "segment_kmeans.py"
    if not script.exists() or not spend_customers:
        return None
    payload = json.dumps({
        "ks": [2, 3, 4, 5],
        "customers": [
            {
                "customer_id": c["customer_id"],
                "monetary": c["total_spent"],
                "frequency": c["frequency"],
                "recency_days": c["recency_days"],
            }
            for c in spend_customers
        ],
    })
    for cmd in (["python", str(script)], ["py", "-3", str(script)]):
        try:
            proc = subprocess.run(cmd, input=payload, capture_output=True, text=True, timeout=20)
            if proc.returncode != 0:
                continue
            decoded = json.loads(proc.stdout or "{}")
            if decoded.get("success"):
                return decoded.get("results")
        except Exception:
            continue
    return None
