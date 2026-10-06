import csv
import html
import io
import json
import math
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import Response
from psycopg2.extras import RealDictCursor

from app.activity import actor_display_name, log_event, write_activity_log
from app.automation import (
    apply_received_lot,
    compute_reorder_suggestions,
    create_purchase_orders_from_needs,
    last_run_status,
    run_daily_jobs,
    run_hourly_jobs,
)
from app.db import fetch_all, fetch_one, get_conn, next_id
from app.deps import require_admin
from app.checkout_rates import get_checkout_rates, save_checkout_rate
from app.loyalty import get_loyalty_settings, save_loyalty_peso
from app.mailer import send_mail
from app.report_files import pdf_bytes, workbook_bytes
from app.routers.admin import _jsonable, _log, _row, _rows, _unauthorized
from app.stock import sync_stock_status_for_drug

router = APIRouter(prefix="/api/admin", tags=["admin"])
ROOT = Path(__file__).resolve().parent.parent.parent
MANILA = ZoneInfo("Asia/Manila")


def _manila_today():
    return datetime.now(MANILA).date()


def _parse_iso_date(value):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


@router.get("/promos")
def list_promos(request: Request):
    if not require_admin(request):
        return _unauthorized()
    today = date.today().isoformat()
    rows = fetch_all(
        """
        SELECT p.promo_id, p.name, p.discount_type, p.discount_value, p.drug_id, p.category,
               p.segment_label, p.start_date, p.end_date, p.is_active, p.created_by, p.created_at,
               d.generic_name, d.brand_name
        FROM promos p
        LEFT JOIN drugs_master d ON p.drug_id = d.drug_id
        ORDER BY p.created_at DESC
        """
    )
    out = []
    promo_ids = []
    for row in rows:
        item = _row(row)
        item["discount_value"] = float(item.get("discount_value") or 0)
        item["is_active"] = int(item.get("is_active") or 0)
        start = str(item.get("start_date") or "")[:10]
        end = str(item.get("end_date") or "")[:10]
        if not item["is_active"]:
            status = "Disabled"
        elif end < today:
            status = "Expired"
        elif start > today:
            status = "Scheduled"
        else:
            status = "Active"
        item["status"] = status
        item["products"] = []
        out.append(item)
        promo_ids.append(int(item["promo_id"]))
    if promo_ids:
        links = fetch_all(
            """
            SELECT pd.promo_id, d.drug_id, d.generic_name, d.brand_name
            FROM promo_drugs pd JOIN drugs_master d ON pd.drug_id = d.drug_id
            WHERE pd.promo_id = ANY(%s) ORDER BY d.generic_name
            """,
            (promo_ids,),
        )
        by_promo = {}
        for link in links:
            label = f"{link['generic_name']}" + (f" ({link['brand_name']})" if link.get("brand_name") else "")
            by_promo.setdefault(int(link["promo_id"]), []).append(label.strip())
        for item in out:
            names = by_promo.get(int(item["promo_id"]), [])
            item["products"] = names
            if names:
                item["scope"] = ", ".join(names) if len(names) <= 2 else f"{names[0]}, {names[1]} +{len(names) - 2} more"
            elif item.get("drug_id"):
                item["scope"] = f"{item.get('generic_name') or ''}" + (
                    f" ({item['brand_name']})" if item.get("brand_name") else ""
                )
            elif item.get("segment_label"):
                item["scope"] = f"Segment: {item['segment_label']}"
            else:
                item["scope"] = item.get("category") or "Storewide"
    return out


@router.post("/promos")
async def add_promo(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    name = str(data.get("name") or "").strip()
    discount_type = "fixed" if data.get("discount_type") == "fixed" else "percent"
    discount_value = float(data["discount_value"]) if data.get("discount_value") is not None else None
    scope = data.get("scope") or "all"
    start_date = data.get("start_date") or ""
    end_date = data.get("end_date") or ""
    category = drug_id = segment_label = None
    drug_ids = []
    if scope == "segment":
        segment_label = str(data.get("segment_label") or "").strip()
        if not segment_label:
            return {"success": False, "message": "Please select a customer segment."}
    elif scope == "category":
        category = str(data.get("category") or "").strip()
        if not category:
            return {"success": False, "message": "Please select a category."}
    elif scope == "drug":
        drug_id = int(data.get("drug_id") or 0)
        if not drug_id:
            return {"success": False, "message": "Please select a drug."}
    elif scope == "drugs":
        drug_ids = [int(x) for x in (data.get("drug_ids") or []) if int(x) > 0]
        drug_ids = list(dict.fromkeys(drug_ids))
        if not drug_ids:
            return {"success": False, "message": "Please select at least one product."}
    if not name or discount_value is None or discount_value <= 0 or not start_date or not end_date:
        return {"success": False, "message": "Name, a positive discount value, and both dates are required."}
    start = _parse_iso_date(start_date)
    end = _parse_iso_date(end_date)
    if not start or not end:
        return {"success": False, "message": "Start and end dates are invalid."}
    today = _manila_today()
    if start < today or end < today:
        return {"success": False, "message": "Promo dates cannot be in the past."}
    if end < start:
        return {"success": False, "message": "End date cannot be before the start date."}
    if discount_type == "percent" and discount_value > 100:
        return {"success": False, "message": "A percent discount cannot exceed 100%."}
    admin_name = request.session.get("user_first_name") or "Admin"
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            promo_id = next_id(cur, "promos", "promo_id")
            cur.execute(
                """
                INSERT INTO promos (promo_id, name, discount_type, discount_value, drug_id, category, segment_label, start_date, end_date, is_active, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)
                """,
                (promo_id, name, discount_type, discount_value, drug_id, category, segment_label, start_date, end_date, admin_name),
            )
            if scope == "drugs":
                for did in drug_ids:
                    cur.execute("INSERT INTO promo_drugs (promo_id, drug_id) VALUES (%s, %s)", (promo_id, did))
            if scope == "segment":
                note = f" for the {segment_label} segment"
            elif scope == "drugs":
                note = f" on {len(drug_ids)} products"
            else:
                note = ""
            amount = f"{discount_value:g}% off" if discount_type == "percent" else f"₱{discount_value:g} off"
            _log(cur, request, "Create Promo", f"Created promo {name}: {amount}, {start_date} to {end_date}{note}.")
    return {"success": True, "promo_id": promo_id}


@router.get("/loyalty-settings")
def get_loyalty_settings_admin(request: Request):
    if not require_admin(request):
        return _unauthorized()
    return {"success": True, **get_loyalty_settings()}


@router.post("/loyalty-settings")
async def update_loyalty_settings(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    try:
        if data.get("peso_per_point") not in (None, ""):
            settings = save_loyalty_peso(data.get("peso_per_point"))
        else:
            settings = save_loyalty_peso(float(data.get("points_percent") or 0) / 100.0)
    except (TypeError, ValueError) as exc:
        return {"success": False, "message": str(exc) if str(exc) else "Enter pesos off per loyalty point."}
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _log(
                cur,
                request,
                "Update Loyalty Points",
                f"Set loyalty discount to ₱{settings['peso_per_point']:.2f} off per point.",
            )
    return {"success": True, **settings}


@router.get("/checkout-rates")
def get_checkout_rates_admin(request: Request):
    if not require_admin(request):
        return _unauthorized()
    return {"success": True, **get_checkout_rates()}


@router.post("/checkout-rates")
async def update_checkout_rates(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    kind = str(data.get("kind") or "").strip()
    try:
        settings = save_checkout_rate(kind, data.get("percent"))
    except (TypeError, ValueError) as exc:
        return {"success": False, "message": str(exc) if str(exc) else "Enter a percent from 0 to 100."}
    label = "Senior / PWD discount" if kind == "sc_pwd" else "VAT"
    saved = settings["sc_pwd_percent"] if kind == "sc_pwd" else settings["vat_percent"]
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            _log(cur, request, "Update Checkout Rate", f"Set {label} to {saved:.2f}%.")
    return {"success": True, **settings}


@router.post("/promos/toggle")
async def toggle_promo(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    promo_id = int(data.get("promo_id") or 0)
    if not promo_id:
        return {"success": False, "message": "Promo ID is required."}
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "UPDATE promos SET is_active = 1 - is_active WHERE promo_id = %s RETURNING name, is_active",
                (promo_id,),
            )
            row = cur.fetchone()
            if row:
                state = "activated" if int(row["is_active"] or 0) else "deactivated"
                _log(cur, request, "Toggle Promo", f"Promo '{row['name']}' was {state}.")
    return {"success": True}


@router.get("/purchase-orders")
def list_pos(request: Request, status: str = ""):
    if not require_admin(request):
        return _unauthorized()
    sql = """
        SELECT po.po_id, po.status, po.order_date, po.expected_date, po.notes, po.created_by, po.created_at,
               s.supplier_id, s.supplier_name
        FROM purchase_orders po
        JOIN suppliers s ON po.supplier_id = s.supplier_id
    """
    params = []
    if status:
        sql += " WHERE po.status = %s"
        params.append(status)
    sql += " ORDER BY po.created_at DESC"
    orders = []
    po_ids = []
    for row in fetch_all(sql, tuple(params) if params else None):
        item = _row(row)
        item["po_id"] = int(item["po_id"])
        item["po_number"] = f"PO-{item['po_id']:05d}"
        item["item_count"] = item["quantity_ordered_total"] = item["quantity_received_total"] = 0
        orders.append(item)
        po_ids.append(item["po_id"])
    if po_ids:
        totals = fetch_all(
            """
            SELECT po_id, COUNT(*) AS item_count,
                   COALESCE(SUM(quantity_ordered), 0) AS qty_ordered,
                   COALESCE(SUM(quantity_received), 0) AS qty_received
            FROM purchase_order_items WHERE po_id = ANY(%s) GROUP BY po_id
            """,
            (po_ids,),
        )
        by_po = {int(t["po_id"]): t for t in totals}
        for order in orders:
            t = by_po.get(order["po_id"])
            if t:
                order["item_count"] = int(t["item_count"])
                order["quantity_ordered_total"] = int(t["qty_ordered"])
                order["quantity_received_total"] = int(t["qty_received"])
    return orders


@router.get("/purchase-orders/{po_id}")
def po_details(po_id: int, request: Request):
    if not require_admin(request):
        return _unauthorized()
    po = fetch_one(
        """
        SELECT po.po_id, po.status, po.order_date, po.expected_date, po.notes, po.created_by, po.created_at,
               s.supplier_id, s.supplier_name, s.contact_number, s.email
        FROM purchase_orders po JOIN suppliers s ON po.supplier_id = s.supplier_id
        WHERE po.po_id = %s
        """,
        (po_id,),
    )
    if not po:
        return {"success": False, "message": "Purchase order not found."}
    po = _row(po)
    po["po_number"] = f"PO-{int(po['po_id']):05d}"
    items = []
    for row in fetch_all(
        """
        SELECT poi.po_item_id, poi.drug_id, poi.quantity_ordered, poi.quantity_received, poi.unit_cost,
               d.generic_name, d.brand_name, d.dosage, d.form, d.category, d.barcode, d.cost_price AS catalog_cost
        FROM purchase_order_items poi JOIN drugs_master d ON poi.drug_id = d.drug_id
        WHERE poi.po_id = %s ORDER BY poi.po_item_id ASC
        """,
        (po_id,),
    ):
        item = _row(row)
        item["quantity_ordered"] = int(item["quantity_ordered"] or 0)
        item["quantity_received"] = int(item["quantity_received"] or 0)
        item["quantity_outstanding"] = max(0, item["quantity_ordered"] - item["quantity_received"])
        items.append(item)
    po["items"] = items
    drug_ids = [int(it["drug_id"]) for it in items if it.get("drug_id")]
    last_by_drug = {}
    if drug_ids:
        for row in fetch_all(
            """
            SELECT DISTINCT ON (il.drug_id)
                il.drug_id, il.lot_number, il.expiration_date, il.cost_price, il.price
            FROM inventory_lots il
            WHERE il.supplier = %s AND il.drug_id = ANY(%s)
            ORDER BY il.drug_id, il.date_added DESC NULLS LAST, il.lot_inventory_id DESC
            """,
            (po["supplier_id"], drug_ids),
        ):
            last_by_drug[int(row["drug_id"])] = row
    today = _manila_today()
    for item in items:
        last = last_by_drug.get(int(item["drug_id"])) or {}
        last_exp = last.get("expiration_date")
        last_exp_s = last_exp.isoformat()[:10] if hasattr(last_exp, "isoformat") else str(last_exp or "")[:10]
        item["last_lot_number"] = last.get("lot_number") or ""
        item["last_expiration_date"] = last_exp_s if last_exp_s and last_exp_s >= today.isoformat() else ""
        cost = last.get("cost_price")
        if cost is None:
            cost = last.get("price")
        if cost is None:
            cost = item.get("unit_cost")
        if cost is None:
            cost = item.get("catalog_cost")
        item["last_cost_price"] = float(cost) if cost not in (None, "") else None
    deliveries = []
    delivery_ids = []
    for row in fetch_all(
        """
        SELECT delivery_id, delivery_date, received_by, reference_number, notes, created_at
        FROM deliveries WHERE po_id = %s ORDER BY created_at DESC
        """,
        (po_id,),
    ):
        d = _row(row)
        d["items"] = []
        deliveries.append(d)
        delivery_ids.append(int(d["delivery_id"]))
    if delivery_ids:
        di_rows = fetch_all(
            """
            SELECT di.delivery_id, di.quantity_received, di.lot_inventory_id, l.lot_number, l.expiration_date,
                   d.generic_name, d.brand_name
            FROM delivery_items di
            JOIN inventory_lots l ON di.lot_inventory_id = l.lot_inventory_id
            JOIN drugs_master d ON l.drug_id = d.drug_id
            WHERE di.delivery_id = ANY(%s)
            ORDER BY di.delivery_item_id ASC
            """,
            (delivery_ids,),
        )
        by_d = {}
        for row in di_rows:
            item = _row(row)
            item["quantity_received"] = int(item.get("quantity_received") or 0)
            by_d.setdefault(int(item["delivery_id"]), []).append(item)
        for d in deliveries:
            d["items"] = by_d.get(int(d["delivery_id"]), [])
    po["deliveries"] = deliveries
    return {"success": True, "po": po}


@router.post("/purchase-orders")
async def create_po(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    supplier_id = int(data.get("supplier_id") or 0)
    if not supplier_id:
        return {"success": False, "message": "Please select a supplier."}
    items = []
    for it in data.get("items") or []:
        drug_id = int(it.get("drug_id") or 0)
        qty = int(it.get("quantity_ordered") or 0)
        unit_cost = float(it["unit_cost"]) if it.get("unit_cost") not in (None, "") else None
        if drug_id and qty > 0:
            items.append((drug_id, qty, unit_cost))
    if not items:
        return {"success": False, "message": "Please add at least one item with a valid quantity."}
    supplier = fetch_one(
        "SELECT supplier_id, supplier_name FROM suppliers WHERE supplier_id = %s AND status = 'Active'",
        (supplier_id,),
    )
    if not supplier:
        return {"success": False, "message": "Please select an active supplier."}
    admin_name = request.session.get("user_first_name") or "Admin"
    order_date = _parse_iso_date(data.get("order_date")) or _manila_today()
    expected_date = _parse_iso_date(data.get("expected_date"))
    today = _manila_today()
    if order_date < today:
        return {"success": False, "message": "Order date cannot be in the past."}
    if expected_date and expected_date < today:
        return {"success": False, "message": "Expected date cannot be in the past."}
    notes = str(data.get("notes") or "").strip() or None
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            po_id = next_id(cur, "purchase_orders", "po_id")
            cur.execute(
                """
                INSERT INTO purchase_orders (po_id, supplier_id, status, order_date, expected_date, notes, created_by)
                VALUES (%s, %s, 'Pending', %s, %s, %s, %s)
                """,
                (po_id, supplier_id, order_date, expected_date, notes, admin_name),
            )
            for drug_id, qty, unit_cost in items:
                item_id = next_id(cur, "purchase_order_items", "po_item_id")
                cur.execute(
                    "INSERT INTO purchase_order_items (po_item_id, po_id, drug_id, quantity_ordered, unit_cost) VALUES (%s, %s, %s, %s, %s)",
                    (item_id, po_id, drug_id, qty, unit_cost),
                )
            po_number = f"PO-{po_id:05d}"
            _log(
                cur,
                request,
                "Create Purchase Order",
                f"Created {po_number} for {supplier['supplier_name']} with {len(items)} item(s).",
            )
    return {"success": True, "po_id": po_id, "po_number": po_number}


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _date_text(value) -> str:
    if value is None or value == "":
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()[:10]
    return str(value)[:10]


def _peso(amount) -> str:
    return f"₱{amount:,.2f}"


def _po_email_subject(po_number: str) -> str:
    return f"Purchase order {po_number}"


def _po_email_html(po_number, supplier_name, _to_email, order_date, expected_date, notes, lines) -> str:
    """One document for the on-screen preview and the message that is sent."""
    body_rows = []
    total = 0.0
    priced = 0
    for line in lines:
        qty = int(line["quantity"])
        cost = line.get("unit_cost")
        if cost is not None:
            total += round(qty * round(float(cost), 2), 2)
            priced += 1
        body_rows.append(
            "<tr>"
            f"<td style=\"padding:8px;border-bottom:1px solid #FFE3B3;\">{_esc(line.get('generic_name') or '—')}</td>"
            f"<td style=\"padding:8px;border-bottom:1px solid #FFE3B3;\">{_esc(line.get('brand_name') or '—')}</td>"
            f"<td style=\"padding:8px;border-bottom:1px solid #FFE3B3;\">{_esc(line.get('dosage') or '—')}</td>"
            f"<td style=\"padding:8px;border-bottom:1px solid #FFE3B3;text-align:right;\">{qty}</td>"
            "</tr>"
        )
    total_txt = _peso(round(total, 2)) if priced else "—"
    rows = "".join(body_rows) or (
        "<tr><td colspan=\"4\" style=\"padding:8px;\">No items</td></tr>"
    )
    th = "padding:8px;text-align:left;font-size:12px;"
    thr = "padding:8px;text-align:right;font-size:12px;"
    greeting_name = (supplier_name or "").strip() or "Supplier"
    return (
        "<div style=\"font-family:Arial,sans-serif;color:#1E3A34;font-size:14px;line-height:1.45;\">"
        f"<p style=\"margin:0 0 8px;\">Good day, {_esc(greeting_name)}.</p>"
        "<p style=\"margin:0 0 16px;\">Please see the purchase order below.</p>"
        f"<h2 style=\"margin:0 0 12px;font-size:20px;\">{_esc(po_number)}</h2>"
        f"<p style=\"margin:0 0 4px;\"><strong>Supplier:</strong> {_esc(supplier_name or '—')}</p>"
        f"<p style=\"margin:0 0 4px;\"><strong>Order date:</strong> {_esc(order_date or '—')}</p>"
        f"<p style=\"margin:0 0 4px;\"><strong>Expected date:</strong> {_esc(expected_date or '—')}</p>"
        f"<p style=\"margin:0 0 14px;\"><strong>Notes:</strong> {_esc(notes or '—')}</p>"
        "<table style=\"width:100%;border-collapse:collapse;\">"
        "<thead><tr style=\"background:#1E3A34;color:#ffffff;\">"
        f"<th style=\"{th}\">Generic name</th>"
        f"<th style=\"{th}\">Brand</th>"
        f"<th style=\"{th}\">Dosage</th>"
        f"<th style=\"{thr}\">Quantity</th>"
        "</tr></thead><tbody>"
        f"{rows}</tbody></table>"
        f"<p style=\"margin:12px 0 0;text-align:right;\"><strong>Grand total:</strong> {_esc(total_txt)}</p>"
        "</div>"
    )


def _email_preview_payload(po_number, supplier_name, to_email, order_date, expected_date, notes, lines) -> dict:
    cleaned = (to_email or "").strip()
    html_doc = _po_email_html(
        po_number, supplier_name, cleaned, order_date, expected_date, notes, lines
    )
    if cleaned:
        return {
            "success": True,
            "can_send": True,
            "to": cleaned,
            "subject": _po_email_subject(po_number),
            "po_number": po_number,
            "html": html_doc,
            "message": "",
        }
    return {
        "success": True,
        "can_send": False,
        "to": "",
        "subject": _po_email_subject(po_number),
        "po_number": po_number,
        "html": html_doc,
        "message": "This supplier has no email. Add it on the supplier record.",
    }


def _lines_for_items(raw_items) -> tuple[list, str | None]:
    wanted = []
    for it in raw_items or []:
        drug_id = int(it.get("drug_id") or 0)
        qty = int(it.get("quantity_ordered") or it.get("quantity") or 0)
        if not drug_id or qty <= 0:
            continue
        cost_raw = it.get("unit_cost")
        cost = None if cost_raw in (None, "") else round(float(cost_raw), 2)
        if cost is not None and cost < 0:
            return [], "Unit cost cannot be negative."
        wanted.append((drug_id, qty, cost))
    if not wanted:
        return [], "Please add at least one item with a valid quantity."
    ids = list(dict.fromkeys(drug_id for drug_id, _, _ in wanted))
    drugs = {
        int(row["drug_id"]): row
        for row in fetch_all(
            "SELECT drug_id, generic_name, brand_name, dosage FROM drugs_master WHERE drug_id = ANY(%s)",
            (ids,),
        )
    }
    lines = []
    for drug_id, qty, cost in wanted:
        drug = drugs.get(drug_id)
        if not drug:
            return [], "One of the items is not in the catalog."
        lines.append({
            "generic_name": drug.get("generic_name") or "",
            "brand_name": drug.get("brand_name") or "",
            "dosage": drug.get("dosage") or "",
            "quantity": qty,
            "unit_cost": cost,
        })
    return lines, None


def _next_po_number() -> str:
    row = fetch_one("SELECT COALESCE(MAX(po_id), 0) + 1 AS nid FROM purchase_orders")
    nid = int(row["nid"] if isinstance(row, dict) else row[0])
    return f"PO-{nid:05d}"


@router.post("/purchase-orders/email-preview")
async def preview_purchase_order_email(request: Request):
    """Build the email document only. Nothing is saved or sent."""
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    try:
        po_id = int(data.get("po_id") or 0)
    except (TypeError, ValueError):
        return {"success": False, "message": "Invalid purchase order."}
    if po_id:
        po = fetch_one(
            """
            SELECT po.po_id, po.order_date, po.expected_date, po.notes, s.supplier_name, s.email
            FROM purchase_orders po JOIN suppliers s ON po.supplier_id = s.supplier_id
            WHERE po.po_id = %s
            """,
            (po_id,),
        )
        if not po:
            return {"success": False, "message": "Purchase order not found."}
        po = _row(po)
        raw_items = []
        for row in fetch_all(
            """
            SELECT poi.drug_id, poi.quantity_ordered, poi.unit_cost
            FROM purchase_order_items poi
            WHERE poi.po_id = %s ORDER BY poi.po_item_id ASC
            """,
            (po_id,),
        ):
            item = _row(row)
            raw_items.append(item)
        lines, err = _lines_for_items(raw_items)
        if err:
            return {"success": False, "message": err}
        return _email_preview_payload(
            f"PO-{int(po['po_id']):05d}",
            po.get("supplier_name") or "",
            po.get("email") or "",
            _date_text(po.get("order_date")),
            _date_text(po.get("expected_date")),
            (po.get("notes") or "").strip(),
            lines,
        )

    supplier_id = int(data.get("supplier_id") or 0)
    if not supplier_id:
        return {"success": False, "message": "Please select a supplier."}
    supplier = fetch_one(
        "SELECT supplier_id, supplier_name, email FROM suppliers WHERE supplier_id = %s AND status = 'Active'",
        (supplier_id,),
    )
    if not supplier:
        return {"success": False, "message": "Please select an active supplier."}
    order_date = _parse_iso_date(data.get("order_date")) or _manila_today()
    expected_date = _parse_iso_date(data.get("expected_date"))
    today = _manila_today()
    if order_date < today:
        return {"success": False, "message": "Order date cannot be in the past."}
    if expected_date and expected_date < today:
        return {"success": False, "message": "Expected date cannot be in the past."}
    lines, err = _lines_for_items(data.get("items") or [])
    if err:
        return {"success": False, "message": err}
    return _email_preview_payload(
        _next_po_number(),
        supplier["supplier_name"],
        supplier.get("email") or "",
        order_date.isoformat(),
        expected_date.isoformat() if expected_date else "",
        str(data.get("notes") or "").strip(),
        lines,
    )


@router.post("/purchase-orders/{po_id}/email")
async def email_purchase_order(po_id: int, request: Request):
    """Send the preview HTML. Does not create a purchase order."""
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    html_doc = str(data.get("html") or "")
    subject = str(data.get("subject") or "").strip()
    po = fetch_one(
        """
        SELECT po.po_id, po.status, s.email, s.supplier_name
        FROM purchase_orders po JOIN suppliers s ON po.supplier_id = s.supplier_id
        WHERE po.po_id = %s
        """,
        (po_id,),
    )
    if not po:
        return {"success": False, "email_sent": False, "message": "Purchase order not found."}
    po = _row(po)
    po_number = f"PO-{int(po['po_id']):05d}"
    email = (po.get("email") or "").strip()
    if not email:
        return {
            "success": False,
            "email_sent": False,
            "message": "This supplier has no email. Add it on the supplier record.",
        }
    expected_subject = _po_email_subject(po_number)
    if subject != expected_subject or po_number not in html_doc:
        return {
            "success": False,
            "email_sent": False,
            "message": "The preview does not match this purchase order. Open it again.",
        }
    sent = send_mail(email, expected_subject, html_doc)
    if sent.get("success"):
        log_event("Email Purchase Order", f"Emailed {po_number} to {email}.", request=request)
        return {
            "success": True,
            "email_sent": True,
            "po_number": po_number,
            "message": f"Emailed {po_number} to {email}.",
        }
    log_event(
        "Email Purchase Order",
        f"Did not email {po_number} to {email}.",
        request=request,
    )
    return {
        "success": True,
        "email_sent": False,
        "po_number": po_number,
        "message": f"{po_number} is still Pending. The email was not sent.",
    }


@router.post("/deliveries/receive")
async def receive_delivery(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    po_id = int(data.get("po_id") or 0)
    if not po_id:
        return {"success": False, "message": "Missing po_id."}
    po_items = {int(r["po_item_id"]): r for r in fetch_all(
        "SELECT po_item_id, drug_id, quantity_ordered, quantity_received FROM purchase_order_items WHERE po_id = %s",
        (po_id,),
    )}
    if not po_items:
        return {"success": False, "message": "Purchase order not found or has no items."}
    to_receive = []
    for it in data.get("items") or []:
        po_item_id = int(it.get("po_item_id") or 0)
        qty = int(it.get("quantity_received") or 0)
        if qty <= 0 or po_item_id not in po_items:
            continue
        lot_number = str(it.get("lot_number") or "").strip()
        expiration_date = it.get("expiration_date") or ""
        if not lot_number or not expiration_date:
            return {"success": False, "message": "Each received item needs a lot number and expiration date."}
        exp = _parse_iso_date(expiration_date)
        if not exp:
            return {"success": False, "message": "Each received item needs a valid expiration date."}
        if exp < _manila_today():
            return {"success": False, "message": "Expiration date cannot be in the past."}
        cost_val = float(it["cost_price"]) if it.get("cost_price") not in (None, "") else None
        if cost_val is None or cost_val <= 0:
            return {"success": False, "message": "Each received item needs a unit cost. Selling price is computed from category markup."}
        to_receive.append({
            "po_item_id": po_item_id,
            "drug_id": int(po_items[po_item_id]["drug_id"]),
            "lot_number": lot_number,
            "expiration_date": expiration_date,
            "quantity_received": qty,
            "price": 0,
            "cost_price": cost_val,
        })
    if not to_receive:
        return {"success": False, "message": "Please enter at least one item with a quantity, lot number, and expiration date."}
    reference = str(data.get("reference_number") or "").strip()
    if not reference:
        return {"success": False, "message": "Invoice / reference number is required."}
    delivery_date = _parse_iso_date(data.get("delivery_date")) or _manila_today()
    if delivery_date < _manila_today():
        return {"success": False, "message": "Delivery date cannot be in the past."}
    admin_name = request.session.get("user_first_name") or "Admin"
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            delivery_id = next_id(cur, "deliveries", "delivery_id")
            cur.execute(
                "INSERT INTO deliveries (delivery_id, po_id, delivery_date, received_by, reference_number, notes) VALUES (%s, %s, %s, %s, %s, %s)",
                (delivery_id, po_id, delivery_date, admin_name, reference, str(data.get("notes") or "").strip() or None),
            )
            created = 0
            updated = 0
            affected = set()
            for it in to_receive:
                try:
                    lot_id, action = apply_received_lot(
                        cur,
                        it["drug_id"],
                        it["lot_number"],
                        it["expiration_date"],
                        it["quantity_received"],
                        it["price"],
                        it["cost_price"],
                        po_id,
                    )
                except Exception:
                    continue
                if action == "created":
                    created += 1
                else:
                    updated += 1
                di_id = next_id(cur, "delivery_items", "delivery_item_id")
                cur.execute(
                    "INSERT INTO delivery_items (delivery_item_id, delivery_id, po_item_id, lot_inventory_id, quantity_received) VALUES (%s, %s, %s, %s, %s)",
                    (di_id, delivery_id, it["po_item_id"], lot_id, it["quantity_received"]),
                )
                cur.execute(
                    "UPDATE purchase_order_items SET quantity_received = quantity_received + %s WHERE po_item_id = %s",
                    (it["quantity_received"], it["po_item_id"]),
                )
                affected.add(it["drug_id"])
            if created == 0 and updated == 0:
                return {"success": False, "message": "No items could be received. Check lot numbers and try again."}
            for drug_id in affected:
                sync_stock_status_for_drug(cur, drug_id)
            cur.execute(
                "SELECT SUM(quantity_ordered) AS ordered, SUM(quantity_received) AS received FROM purchase_order_items WHERE po_id = %s",
                (po_id,),
            )
            totals = cur.fetchone()
            ordered = int(totals["ordered"] or 0)
            received = int(totals["received"] or 0)
            new_status = "Pending" if received <= 0 else ("Received" if received >= ordered else "Partially Received")
            cur.execute("UPDATE purchase_orders SET status = %s WHERE po_id = %s", (new_status, po_id))
            po_number = f"PO-{po_id:05d}"
            _log(
                cur,
                request,
                "Receive Delivery",
                f"Received {po_number}: {created} new lot(s), {updated} existing lot(s) topped up. Status is now {new_status}.",
            )
    return {
        "success": True,
        "delivery_id": delivery_id,
        "lots_created": created,
        "lots_updated": updated,
        "po_status": new_status,
    }


@router.get("/sales-analytics")
def sales_analytics(request: Request, year: int = 0):
    if not require_admin(request):
        return _unauthorized()
    year = year or date.today().year
    if year < 2000 or year > 2100:
        year = date.today().year
    monthly_rows = {
        int(r["m"]): float(r["total"] or 0)
        for r in fetch_all(
            """
            SELECT EXTRACT(MONTH FROM date_created) AS m, COALESCE(SUM(total_amount), 0) AS total
            FROM sales WHERE EXTRACT(YEAR FROM date_created) = %s AND status = 'completed'
            GROUP BY EXTRACT(MONTH FROM date_created)
            """,
            (year,),
        )
    }
    monthly_labels = []
    monthly_data = []
    for m in range(1, 13):
        monthly_labels.append(datetime(year, m, 1).strftime("%b %Y"))
        monthly_data.append(monthly_rows.get(m, 0))
    cat_rows = fetch_all(
        """
        SELECT d.category, COALESCE(SUM(si.quantity), 0) AS total_qty
        FROM sales_items si JOIN sales s ON si.sale_id = s.sale_id JOIN drugs_master d ON si.drug_id = d.drug_id
        WHERE EXTRACT(YEAR FROM s.date_created) = %s AND s.status = 'completed'
        GROUP BY d.category ORDER BY total_qty DESC LIMIT 8
        """,
        (year,),
    )
    seasonal_rows = {
        int(r["m"]): float(r["total"] or 0)
        for r in fetch_all(
            """
            SELECT EXTRACT(MONTH FROM date_created) AS m, COALESCE(SUM(total_amount), 0) AS total
            FROM sales WHERE status = 'completed'
            GROUP BY EXTRACT(MONTH FROM date_created)
            """
        )
    }
    month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    seasonal_labels = month_names
    seasonal_data = [seasonal_rows.get(m, 0) for m in range(1, 13)]
    return {
        "year": year,
        "monthly_labels": monthly_labels,
        "monthly_data": monthly_data,
        "category_labels": [str(r["category"] or "Other").title() for r in cat_rows],
        "category_data": [int(r["total_qty"] or 0) for r in cat_rows],
        "seasonal_labels": seasonal_labels,
        "seasonal_data": seasonal_data,
    }


@router.get("/activity-logs")
def activity_logs(
    request: Request,
    limit: int = 20,
    offset: int = 0,
    days: int = 0,
    start: str = "",
    end: str = "",
    q: str = "",
):
    if not require_admin(request):
        return _unauthorized()
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))
    where = ["action <> 'UI Click'", "action NOT ILIKE %s"]
    params: list = ["%click%"]
    start_s = str(start or "").strip()[:10]
    end_s = str(end or "").strip()[:10]
    if start_s and end_s and start_s > end_s:
        start_s, end_s = end_s, start_s
    if start_s and end_s:
        where.append("date::date BETWEEN %s AND %s")
        params.extend([start_s, end_s])
    elif int(days or 0) > 0:
        span = max(1, min(365, int(days)))
        where.append("date::date >= (CURRENT_DATE - (%s - 1))")
        params.append(span)
    query = " ".join(str(q or "").split())[:80]
    if query:
        like = f"%{query}%"
        where.append("(admin_name ILIKE %s OR action ILIKE %s OR details ILIKE %s)")
        params.extend([like, like, like])
    clause = " WHERE " + " AND ".join(where)
    total_row = fetch_one("SELECT COUNT(*) AS n FROM activity_logs" + clause, tuple(params))
    total = int((total_row or {}).get("n") or 0)
    logs = []
    for row in fetch_all(
        "SELECT date, admin_name, action, details FROM activity_logs"
        + clause
        + " ORDER BY date DESC LIMIT %s OFFSET %s",
        tuple(params) + (limit, offset),
    ):
        dt = row["date"]
        if hasattr(dt, "strftime"):
            ds = dt.strftime("%Y-%m-%d %H:%M")
        else:
            ds = str(dt)[:16]
        logs.append({"date": ds, "admin_name": row["admin_name"], "action": row["action"], "details": row["details"]})
    return {"logs": logs, "total": total, "limit": limit, "offset": offset}


@router.get("/moving-items")
def moving_items(request: Request):
    if not require_admin(request):
        return _unauthorized()
    days = 30
    fast = []
    for row in fetch_all(
        """
        SELECT d.generic_name, d.brand_name, COALESCE(SUM(si.quantity), 0) AS qty_sold
        FROM sales_items si JOIN sales s ON si.sale_id = s.sale_id JOIN drugs_master d ON si.drug_id = d.drug_id
        WHERE s.date_created >= (CURRENT_DATE - (%s * INTERVAL '1 day')) AND s.status = 'completed'
        GROUP BY d.drug_id, d.generic_name, d.brand_name ORDER BY qty_sold DESC LIMIT 8
        """,
        (days,),
    ):
        fast.append({
            "name": f"{row['generic_name']}" + (f" ({row['brand_name']})" if row.get("brand_name") else ""),
            "qty_sold": int(row["qty_sold"] or 0),
        })
    slow = []
    for row in fetch_all(
        """
        SELECT d.generic_name, d.brand_name, d.stock_status,
               COALESCE(SUM(il.current_stock), 0) AS on_hand, COALESCE(recent.sold, 0) AS recent_sold
        FROM drugs_master d
        LEFT JOIN inventory_lots il ON d.drug_id = il.drug_id AND il.is_active = 1 AND il.expiration_date >= CURRENT_DATE
        LEFT JOIN (
            SELECT si.drug_id, SUM(si.quantity) AS sold FROM sales_items si
            JOIN sales s ON si.sale_id = s.sale_id
            WHERE s.date_created >= (CURRENT_DATE - INTERVAL '90 days') AND s.status = 'completed'
            GROUP BY si.drug_id
        ) recent ON d.drug_id = recent.drug_id
        WHERE d.is_active = 1
        GROUP BY d.drug_id, d.generic_name, d.brand_name, d.stock_status, recent.sold
        HAVING COALESCE(SUM(il.current_stock), 0) > 0 AND COALESCE(recent.sold, 0) <= 2
        ORDER BY on_hand DESC, recent_sold ASC LIMIT 8
        """
    ):
        slow.append({
            "name": f"{row['generic_name']}" + (f" ({row['brand_name']})" if row.get("brand_name") else ""),
            "on_hand": int(row["on_hand"] or 0),
            "recent_sold": int(row["recent_sold"] or 0),
            "stock_status": row.get("stock_status") or "ok",
        })
    return {"fast_moving": fast, "slow_moving": slow}


@router.get("/expiring")
def expiring(request: Request, days: int = 30):
    if not require_admin(request):
        return _unauthorized()
    days = max(1, days)
    items = []
    total = 0.0
    for row in fetch_all(
        """
        SELECT l.lot_inventory_id, l.lot_number, l.expiration_date, l.current_stock, l.price,
               d.generic_name, d.brand_name, d.dosage, d.form,
               (l.expiration_date - CURRENT_DATE) AS days_left
        FROM inventory_lots l JOIN drugs_master d ON l.drug_id = d.drug_id
        WHERE l.is_active = 1 AND l.current_stock > 0
          AND l.expiration_date >= CURRENT_DATE
          AND l.expiration_date <= (CURRENT_DATE + (%s || ' days')::interval)
        ORDER BY l.expiration_date ASC
        """,
        (days,),
    ):
        qty = int(row["current_stock"] or 0)
        price = float(row["price"] or 0)
        value = qty * price
        total += value
        items.append({
            "lot_inventory_id": int(row["lot_inventory_id"]),
            "generic_name": row["generic_name"],
            "brand_name": row["brand_name"],
            "dosage": row["dosage"],
            "form": row["form"],
            "lot_number": row["lot_number"],
            "expiration_date": _jsonable(row["expiration_date"]),
            "days_left": int(row["days_left"] or 0),
            "current_stock": qty,
            "price": price,
            "value_at_risk": value,
        })
    return {"days": days, "count": len(items), "total_value_at_risk": round(total, 2), "items": items}


@router.get("/reorder-suggestions")
def reorder_suggestions(request: Request):
    if not require_admin(request):
        return _unauthorized()
    return compute_reorder_suggestions()


@router.post("/reorder-suggestions/create-pos")
async def create_pos_from_reorder(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json()
    try:
        drug_ids = [int(x) for x in (data.get("drug_ids") or [])]
    except (TypeError, ValueError):
        return {"success": False, "message": "Invalid drug selection."}
    drug_ids = [d for d in drug_ids if d > 0]
    if not drug_ids:
        return {"success": False, "message": "Select at least one item that needs reorder."}

    suggestions = {int(s["drug_id"]): s for s in compute_reorder_suggestions().get("suggestions") or []}
    needs = []
    skipped = []
    for drug_id in drug_ids:
        row = suggestions.get(drug_id)
        if not row:
            skipped.append({"name": f"Drug #{drug_id}", "reason": "not in current suggestions"})
            continue
        label = row.get("generic_name") or f"Drug #{drug_id}"
        if str(row.get("procurement_type") or "purchase") == "consignment":
            skipped.append({"name": label, "reason": "consignment - create that PO manually"})
            continue
        if row.get("action") != "increase" or int(row.get("suggested_qty") or 0) <= 0:
            skipped.append({"name": label, "reason": "does not need reorder"})
            continue
        needs.append(row)

    actor = actor_display_name(request, fallback="Admin")
    notes = "Created from Reorder Suggestions. Review quantities before sending to the supplier."
    result = create_purchase_orders_from_needs(needs, actor, notes, raise_alerts=False)
    skipped.extend(result["skipped"])
    if not result["created"] and not skipped:
        return {"success": False, "message": "Nothing to create."}
    return {
        "success": True,
        "created": result["created"],
        "skipped": skipped,
    }


@router.get("/automation/status")
def automation_status(request: Request):
    if not require_admin(request):
        return _unauthorized()
    return {"success": True, **last_run_status()}


@router.post("/automation/run")
async def automation_run(request: Request):
    if not require_admin(request):
        return _unauthorized()
    data = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    mode = str((data or {}).get("mode") or "hourly").lower()
    if mode == "daily":
        result = run_daily_jobs()
    else:
        result = run_hourly_jobs()
    with get_conn() as conn:
        with conn.cursor() as cur:
            write_activity_log(cur, "Run Automation", f"Ran {mode} jobs.", request=request)
    return {"success": True, "mode": mode, "result": result}


def _shift_month(day: date, delta: int) -> date:
    month_index = day.year * 12 + (day.month - 1) + delta
    return date(month_index // 12, month_index % 12 + 1, 1)


def _parse_month(value: str) -> date | None:
    text = str(value or "").strip()
    if len(text) < 7 or text[4] != "-":
        return None
    try:
        return date(int(text[0:4]), int(text[5:7]), 1)
    except ValueError:
        return None


@router.get("/monthly-consumption")
def monthly_consumption(request: Request, months: int = 6, start: str = "", end: str = ""):
    if not require_admin(request):
        return _unauthorized()
    today = date.today().replace(day=1)
    end_month = _parse_month(end) or today
    if end_month > today:
        end_month = today
    start_month = _parse_month(start) or _shift_month(end_month, -(max(1, min(24, months)) - 1))
    if start_month > end_month:
        start_month = end_month
    earliest = _shift_month(end_month, -23)
    if start_month < earliest:
        start_month = earliest
    labels = []
    cursor = start_month
    while cursor <= end_month:
        labels.append(f"{cursor.year:04d}-{cursor.month:02d}")
        cursor = _shift_month(cursor, 1)
    range_end = _shift_month(end_month, 1)
    raw = fetch_all(
        """
        SELECT d.drug_id, d.generic_name, d.brand_name, d.dosage, d.form, d.category,
               to_char(s.date_created, 'YYYY-MM') AS month_label, SUM(si.quantity) AS qty
        FROM sales_items si JOIN sales s ON si.sale_id = s.sale_id JOIN drugs_master d ON si.drug_id = d.drug_id
        WHERE s.status = 'completed'
          AND s.date_created >= %s
          AND s.date_created < %s
        GROUP BY d.drug_id, d.generic_name, d.brand_name, d.dosage, d.form, d.category, month_label
        """,
        (start_month, range_end),
    )
    by_drug = {}
    for row in raw:
        did = int(row["drug_id"])
        if did not in by_drug:
            by_drug[did] = {
                "drug_id": did,
                "generic_name": row["generic_name"],
                "brand_name": row["brand_name"],
                "dosage": row["dosage"],
                "form": row["form"],
                "category": row["category"],
                "monthly": {lab: 0 for lab in labels},
                "total": 0,
            }
        qty = int(row["qty"] or 0)
        if row["month_label"] in by_drug[did]["monthly"]:
            by_drug[did]["monthly"][row["month_label"]] = qty
        by_drug[did]["total"] += qty
    rows = sorted(by_drug.values(), key=lambda r: r["total"], reverse=True)
    monthly_totals = {lab: 0 for lab in labels}
    for r in rows:
        for m, qty in r["monthly"].items():
            monthly_totals[m] += qty
    return {"months": labels, "monthly_totals": monthly_totals, "rows": rows}


@router.get("/procurement")
def procurement(request: Request):
    if not require_admin(request):
        return _unauthorized()
    summary = {"total_inventory_cost": 0, "total_units": 0, "supplier_count": 0}
    srow = fetch_one(
        """
        SELECT COALESCE(SUM(COALESCE(cost_price, price, 0) * current_stock), 0) AS total_cost,
               COALESCE(SUM(current_stock), 0) AS total_units
        FROM inventory_lots
        WHERE is_active = 1 AND expiration_date >= CURRENT_DATE
        """
    )
    summary["total_inventory_cost"] = round(float(srow["total_cost"] or 0), 2)
    summary["total_units"] = int(srow["total_units"] or 0)
    by_supplier = []
    for row in fetch_all(
        """
        SELECT s.supplier_id, s.supplier_name,
               COALESCE(SUM(COALESCE(il.cost_price, il.price, 0) * il.current_stock), 0) AS total_cost,
               COALESCE(SUM(il.current_stock), 0) AS total_units, COUNT(DISTINCT il.drug_id) AS distinct_drugs
        FROM suppliers s JOIN inventory_lots il ON il.supplier = s.supplier_id AND il.is_active = 1
          AND il.expiration_date >= CURRENT_DATE
        GROUP BY s.supplier_id, s.supplier_name
        HAVING COALESCE(SUM(il.current_stock), 0) > 0
        ORDER BY total_cost DESC
        """
    ):
        by_supplier.append({
            "supplier_id": int(row["supplier_id"]),
            "supplier_name": row["supplier_name"],
            "total_cost": round(float(row["total_cost"] or 0), 2),
            "total_units": int(row["total_units"] or 0),
            "distinct_drugs": int(row["distinct_drugs"] or 0),
        })
    summary["supplier_count"] = len(by_supplier)
    by_category = [
        {
            "category": r["category"],
            "total_cost": round(float(r["total_cost"] or 0), 2),
            "total_units": int(r["total_units"] or 0),
        }
        for r in fetch_all(
            """
            SELECT d.category,
                   COALESCE(SUM(COALESCE(il.cost_price, il.price, 0) * il.current_stock), 0) AS total_cost,
                   COALESCE(SUM(il.current_stock), 0) AS total_units
            FROM drugs_master d JOIN inventory_lots il ON il.drug_id = d.drug_id AND il.is_active = 1
              AND il.expiration_date >= CURRENT_DATE
            GROUP BY d.category HAVING COALESCE(SUM(il.current_stock), 0) > 0 ORDER BY total_cost DESC
            """
        )
    ]
    return {"summary": summary, "by_supplier": by_supplier, "by_category": by_category}


def _linreg(xs, ys):
    n = len(xs)
    if n == 0:
        return 0, 0
    if n == 1:
        return 0, ys[0]
    sum_x, sum_y = sum(xs), sum(ys)
    sum_xy = sum(xs[i] * ys[i] for i in range(n))
    sum_xx = sum(x * x for x in xs)
    denom = n * sum_xx - sum_x * sum_x
    if denom == 0:
        return 0, sum_y / n
    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n
    return slope, intercept


def _mean(vals):
    return (sum(vals) / len(vals)) if vals else 0.0


def _window_mean(series, last_n):
    if not series:
        return 0.0
    chunk = series[-last_n:] if last_n else series
    return _mean(chunk)


def _run_rate(ys):
    """Daily level that does not collapse to 0 just because most calendar days are quiet."""
    for window in (28, 90):
        level = _window_mean(ys, window)
        if level >= 1:
            return level
    filled_mean = _mean(ys)
    if filled_mean >= 1:
        return filled_mean
    positive = [y for y in ys if y > 0]
    if positive:
        return _mean(positive[-30:])
    return 0.0


def _dow_factors(dates, ys):
    sums = [0.0] * 7
    counts = [0] * 7
    overall = _mean([y for y in ys if y > 0]) or _mean(ys) or 1.0
    for d, y in zip(dates, ys):
        if y <= 0:
            continue
        php_dow = int(datetime.strptime(d, "%Y-%m-%d").strftime("%w"))
        sums[php_dow] += y / overall
        counts[php_dow] += 1
    return [sums[i] / counts[i] if counts[i] else 1.0 for i in range(7)]


def _project_daily(dates, ys, today, period):
    level = _run_rate(ys)
    last14 = _window_mean(ys, 14)
    prev14 = _mean(ys[-28:-14]) if len(ys) >= 28 else last14
    daily_trend = 0.0
    if level >= 1 and last14 >= 1 and prev14 >= 1:
        daily_trend = (last14 - prev14) / 14.0
        cap = level / max(period, 1)
        daily_trend = max(-cap, min(cap, daily_trend))
    factors = _dow_factors(dates, ys)
    labels, values, lower, upper = [], [], [], []
    total = 0.0
    for step in range(1, period + 1):
        future = today + timedelta(days=step)
        php_dow = int(future.strftime("%w"))
        pred = max(0.0, (level + daily_trend * step) * factors[php_dow])
        labels.append(future.strftime("%b %d").replace(" 0", " "))
        values.append(round(pred, 2))
        lower.append(round(pred * 0.8, 2))
        upper.append(round(pred * 1.2, 2))
        total += pred
    return labels, values, lower, upper, total


def _date_range(start: date, end: date):
    out = []
    cur = start
    while cur <= end:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


@router.get("/forecast")
def forecast(request: Request, period: int = 30):
    if not require_admin(request):
        return _unauthorized()
    if period not in (7, 30, 90):
        period = 30
    history_days = 1095
    history = []
    for row in fetch_all(
        """
        SELECT sale_date, SUM(daily_total) AS daily_total, SUM(daily_qty) AS daily_qty
        FROM (
            SELECT s.date_created::date AS sale_date,
                   SUM(s.total_amount)::float AS daily_total,
                   COALESCE(SUM(q.qty), 0)::float AS daily_qty
            FROM sales s
            LEFT JOIN (
                SELECT sale_id, SUM(quantity) AS qty FROM sales_items GROUP BY sale_id
            ) q ON q.sale_id = s.sale_id
            WHERE LOWER(TRIM(s.status)) = 'completed'
              AND s.date_created::date >= (CURRENT_DATE - %s)
            GROUP BY s.date_created::date
            UNION ALL
            SELECT co.order_date::date AS sale_date,
                   SUM(co.total_amount)::float AS daily_total,
                   COALESCE(SUM(od.quantity), 0)::float AS daily_qty
            FROM customer_orders co
            LEFT JOIN order_details od ON od.order_id = co.order_id
            WHERE LOWER(TRIM(co.order_status)) IN ('completed', 'ready for pickup')
              AND co.order_date::date >= (CURRENT_DATE - %s)
            GROUP BY co.order_date::date
        ) t
        GROUP BY sale_date
        ORDER BY sale_date ASC
        """,
        (history_days, history_days),
    ):
        history.append({
            "date": _jsonable(row["sale_date"]),
            "total": float(row["daily_total"] or 0),
            "qty": int(row["daily_qty"] or 0),
        })
    if len(history) < 2:
        return {
            "success": True,
            "insufficient_data": True,
            "message": "Not enough sales history yet to generate a reliable forecast. Record at least 2 days of completed sales first.",
            "history_days": len(history),
            "engine": "none",
        }
    per_drug = {}
    for row in fetch_all(
        """
        SELECT drug_id, generic_name, brand_name, category, sale_date, SUM(daily_qty) AS daily_qty
        FROM (
            SELECT d.drug_id, d.generic_name, d.brand_name, d.category,
                   s.date_created::date AS sale_date, SUM(si.quantity) AS daily_qty
            FROM sales_items si
            JOIN sales s ON si.sale_id = s.sale_id
            JOIN drugs_master d ON si.drug_id = d.drug_id
            WHERE LOWER(TRIM(s.status)) = 'completed'
              AND s.date_created::date >= (CURRENT_DATE - %s)
            GROUP BY d.drug_id, d.generic_name, d.brand_name, d.category, s.date_created::date
            UNION ALL
            SELECT d.drug_id, d.generic_name, d.brand_name, d.category,
                   co.order_date::date AS sale_date, SUM(od.quantity) AS daily_qty
            FROM order_details od
            JOIN customer_orders co ON od.order_id = co.order_id
            JOIN drugs_master d ON od.drug_id = d.drug_id
            WHERE LOWER(TRIM(co.order_status)) IN ('completed', 'ready for pickup')
              AND co.order_date::date >= (CURRENT_DATE - %s)
            GROUP BY d.drug_id, d.generic_name, d.brand_name, d.category, co.order_date::date
        ) t
        GROUP BY drug_id, generic_name, brand_name, category, sale_date
        ORDER BY drug_id, sale_date ASC
        """,
        (history_days, history_days),
    ):
        did = int(row["drug_id"])
        if did not in per_drug:
            label = f"{row['generic_name']}" + (f" ({row['brand_name']})" if row.get("brand_name") else "")
            per_drug[did] = {"name": label, "category": row["category"], "series": []}
        per_drug[did]["series"].append({"date": _jsonable(row["sale_date"]), "qty": int(row["daily_qty"] or 0)})
    today = _manila_today()
    first_sale = min(date.fromisoformat(str(p["date"])[:10]) for p in history)
    range_start = max(first_sale, today - timedelta(days=history_days))
    payload = json.dumps({
        "period": period,
        "today": today.isoformat(),
        "range_start": range_start.isoformat(),
        "daily_sales": history,
        "items": per_drug,
    }, default=str)
    script = ROOT / "python" / "forecast_prophet.py"
    if script.exists():
        for cmd in (["python", str(script)], ["py", "-3", str(script)]):
            try:
                proc = subprocess.run(cmd, input=payload, capture_output=True, text=True, timeout=90)
                if proc.returncode != 0:
                    continue
                decoded = json.loads(proc.stdout or "{}")
                if decoded.get("success") and not decoded.get("insufficient_data"):
                    vals = (decoded.get("forecast") or {}).get("values") or []
                    if vals and max(float(v or 0) for v in vals) > 0:
                        return decoded
            except Exception:
                continue
    dates = _date_range(range_start, today)
    total_by = {}
    qty_by = {}
    for p in history:
        total_by[p["date"]] = total_by.get(p["date"], 0) + p["total"]
        qty_by[p["date"]] = qty_by.get(p["date"], 0) + p["qty"]
    sales_ys = [total_by.get(d, 0.0) for d in dates]
    qty_ys = [qty_by.get(d, 0.0) for d in dates]
    labels, values, lower, upper, pred_sales = _project_daily(dates, sales_ys, today, period)
    pred_qty = _run_rate(qty_ys) * period
    item_forecasts = []
    cat_tot = {}
    for info in per_drug.values():
        qmap = {}
        for pt in info["series"]:
            qmap[pt["date"]] = qmap.get(pt["date"], 0) + pt["qty"]
        ys = [qmap.get(d, 0.0) for d in dates]
        predicted = round(_run_rate(ys) * period)
        item_forecasts.append({"name": info["name"], "predicted_qty": predicted})
        cat = info["category"] or "Uncategorized"
        cat_tot[cat] = cat_tot.get(cat, 0) + predicted
    item_forecasts.sort(key=lambda x: x["predicted_qty"], reverse=True)
    top = item_forecasts[:5]
    top_cat = max(cat_tot, key=cat_tot.get) if cat_tot else "N/A"
    return {
        "success": True,
        "insufficient_data": False,
        "period": period,
        "forecast": {"labels": labels, "values": values, "lower": lower, "upper": upper},
        "predicted_total_sales": round(pred_sales, 2),
        "predicted_items_sold": int(round(pred_qty)),
        "top_category": top_cat,
        "top_items": {"labels": [t["name"] for t in top], "data": [t["predicted_qty"] for t in top]},
        "history_days": len(history),
        "engine": "linear_regression_fallback",
    }


SALES_TEMPLATE_HEADERS = ["Date", "Medicine", "Brand", "Quantity", "Price"]


def _file_response(content: bytes, filename: str, kind: str):
    media = "application/pdf" if kind == "pdf" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return Response(
        content=content,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _norm_key(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _is_header_row(cells):
    keys = {_norm_key(c) for c in cells if c not in (None, "")}
    has_date = bool(keys & {"date", "sale_date", "order_date"})
    has_qty = bool(keys & {"quantity", "qty", "units"})
    has_drug = bool(keys & {"drug_id", "id", "generic_name", "generic", "drug", "medicine"})
    return has_date and (has_qty or has_drug)


def _parse_upload_rows(raw: bytes, filename: str):
    name = (filename or "").lower()
    rows = []
    if name.endswith(".xlsx") or name.endswith(".xls"):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
        ws = wb.active
        raw_rows = list(ws.iter_rows(values_only=True))
        header_idx = next((i for i, values in enumerate(raw_rows) if values and _is_header_row(values)), None)
        if header_idx is None:
            return []
        headers = [_norm_key(c) for c in raw_rows[header_idx]]
        for values in raw_rows[header_idx + 1:]:
            if not values or all(v is None or str(v).strip() == "" for v in values):
                continue
            rows.append({headers[i]: values[i] for i in range(min(len(headers), len(values)))})
        return rows
    text = raw.decode("utf-8-sig")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    header_idx = next((i for i, ln in enumerate(lines) if _is_header_row(next(csv.reader([ln])))), 0)
    reader = csv.DictReader(io.StringIO("\n".join(lines[header_idx:])))
    for row in reader:
        rows.append({_norm_key(k): v for k, v in row.items()})
    return rows


def _parse_sale_date(row):
    value = _cell(row, "date", "sale_date", "order_date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        from openpyxl.utils.datetime import from_excel
        return from_excel(value).date()
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing date")
    head = text[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d"):
        try:
            return datetime.strptime(head if fmt == "%Y-%m-%d" else text[:10], fmt).date()
        except ValueError:
            continue
    if len(text) >= 10 and text[2] in "/-" and int(text[:2]) > 12:
        return datetime.strptime(text[:10], "%d/%m/%Y" if "/" in text[:10] else "%d-%m-%Y").date()
    raise ValueError("invalid date")


def _cell(row, *names):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return ""


def _whole_number(value):
    if isinstance(value, bool):
        raise ValueError("not a number")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError("not a whole number")
        return int(value)
    text = str(value or "").strip().replace(",", "")
    if not re.fullmatch(r"\d+", text):
        raise ValueError("not a whole number")
    return int(text)


def _money(value):
    if isinstance(value, bool):
        raise ValueError("not a price")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip().replace(",", "").replace("₱", "").replace("PHP", "")
    if not text:
        raise ValueError("missing price")
    return float(text)


def _inventory_label(drug):
    name = str(drug.get("generic_name") or "").strip()
    brand = str(drug.get("brand_name") or "").strip()
    return f"{name} ({brand})" if brand else name


def _match_inventory_drug(row, by_id, by_generic, by_brand):
    problems = []
    generic_raw = str(_cell(row, "generic_name", "generic", "drug", "medicine")).strip()
    brand_raw = str(_cell(row, "brand_name", "brand")).strip()
    generic = generic_raw.lower()
    brand = brand_raw.lower()
    drug = None
    drug_id_raw = _cell(row, "drug_id", "id")
    if drug_id_raw not in ("", None):
        try:
            drug = by_id.get(int(float(drug_id_raw)))
        except (TypeError, ValueError):
            drug = None
        if drug is None:
            problems.append("that drug id is not in the inventory")
    generic_hits = by_generic.get(generic) or []
    if generic and brand:
        named = next((d for d in generic_hits if str(d.get("brand_name") or "").strip().lower() == brand), None)
        if named is None:
            known = [str(d.get("brand_name")).strip() for d in generic_hits if str(d.get("brand_name") or "").strip()]
            hint = f" Use {', '.join(known)}" if known else ""
            problems.append(f"brand \"{brand_raw}\" does not match {generic_raw} in the inventory.{hint}")
        elif drug is not None and int(drug["drug_id"]) != int(named["drug_id"]):
            problems.append(f"{generic_raw} ({brand_raw}) does not match that drug id")
        else:
            drug = named
    elif generic and not brand:
        if len(generic_hits) == 1:
            drug = drug or generic_hits[0]
        elif generic_hits:
            known = [str(d.get("brand_name")).strip() or generic_raw for d in generic_hits]
            problems.append(f"add the brand for {generic_raw}. In the inventory: {', '.join(known)}")
        else:
            brand_hits = by_brand.get(generic) or []
            if len(brand_hits) == 1:
                drug = drug or brand_hits[0]
            else:
                problems.append(f"{generic_raw} is not in the inventory")
    elif brand and not generic:
        brand_hits = by_brand.get(brand) or []
        if len(brand_hits) == 1:
            drug = drug or brand_hits[0]
        elif brand_hits:
            problems.append(f"brand \"{brand_raw}\" matches more than one medicine. Add the medicine name")
        else:
            problems.append(f"{brand_raw} is not in the inventory")
    elif drug is None:
        problems.append("medicine is required")
    return drug, problems


@router.get("/reports/sales-template")
def sales_template(request: Request):
    if not require_admin(request):
        return _unauthorized()
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = "Sales"
    fill = PatternFill("solid", fgColor="1E3A8A")
    font = Font(color="FFFFFF", bold=True)
    for col, header in enumerate(SALES_TEMPLATE_HEADERS, 1):
        cell = ws.cell(1, col, header)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(horizontal="left")
        ws.column_dimensions[cell.column_letter].width = 16
    ws.append(["2026-08-01", "Paracetamol", "Biogesic", 10, 5.50])
    ws.append(["2026-08-02", "Amoxicillin", "", 4, 12.00])
    buf = io.BytesIO()
    wb.save(buf)
    return _file_response(buf.getvalue(), "pharmalink-sales-template.xlsx", "xlsx")


@router.post("/reports/sales-upload")
async def sales_upload(request: Request, file: UploadFile = File(...)):
    if not require_admin(request):
        return _unauthorized()
    raw = await file.read()
    if not raw:
        return {"success": False, "message": "The file is empty."}
    try:
        parsed = _parse_upload_rows(raw, file.filename or "sales.csv")
    except Exception:
        return {"success": False, "message": "Could not read that file. Use the Excel template or a CSV with the same columns."}
    if not parsed:
        return {"success": False, "message": "No data rows found. Check the header row and try again."}

    drugs = fetch_all("SELECT drug_id, generic_name, brand_name FROM drugs_master")
    by_id = {int(d["drug_id"]): d for d in drugs}
    by_generic = {}
    by_brand = {}
    for d in drugs:
        generic = str(d["generic_name"] or "").strip().lower()
        brand = str(d["brand_name"] or "").strip().lower()
        if generic:
            by_generic.setdefault(generic, []).append(d)
        if brand:
            by_brand.setdefault(brand, []).append(d)
    grouped = {}
    errors = []
    today = _manila_today()
    for idx, row in enumerate(parsed, start=2):
        row_errors = []
        try:
            sale_day = _parse_sale_date(row)
            if sale_day > today:
                row_errors.append(f"Row {idx}: date is in the future.")
        except Exception:
            sale_day = None
            row_errors.append(f"Row {idx}: date must look like 2026-08-01.")
        try:
            qty = _whole_number(_cell(row, "quantity", "qty", "units"))
            if qty <= 0:
                raise ValueError("quantity")
        except (TypeError, ValueError):
            qty = 0
            row_errors.append(f"Row {idx}: quantity must be a whole number greater than 0.")
        try:
            price = _money(_cell(row, "unit_price", "price", "selling_price"))
            if price <= 0:
                raise ValueError("price")
        except (TypeError, ValueError):
            price = 0.0
            row_errors.append(f"Row {idx}: price must be a number greater than 0.")
        drug, match_problems = _match_inventory_drug(row, by_id, by_generic, by_brand)
        for problem in match_problems:
            row_errors.append(f"Row {idx}: {problem}.")
        if row_errors:
            errors.extend(row_errors)
            continue
        grouped.setdefault(sale_day, []).append({
            "drug_id": int(drug["drug_id"]),
            "qty": qty,
            "price": price,
            "subtotal": round(price * qty, 2),
            "label": _inventory_label(drug),
        })

    if errors:
        return {
            "success": False,
            "message": "Upload stopped. Nothing was saved. Fix the file and try again.",
            "errors": errors[:20],
            "error_count": len(errors),
        }
    if not grouped:
        return {"success": False, "message": "No data rows found. Check the header row and try again."}

    user_id = require_admin(request)
    imported_sales = 0
    imported_items = 0
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                lot_optional = True
                try:
                    cur.execute("ALTER TABLE sales_items ALTER COLUMN lot_id DROP NOT NULL")
                except Exception:
                    conn.rollback()
                    lot_optional = False
                for sale_day, items in grouped.items():
                    for item in items:
                        cur.execute(
                            "SELECT lot_inventory_id FROM inventory_lots WHERE drug_id = %s ORDER BY lot_inventory_id LIMIT 1",
                            (item["drug_id"],),
                        )
                        lot = cur.fetchone()
                        if not lot and not lot_optional:
                            raise ValueError(f"{item['label']} is not in the inventory.")
                        item["lot_id"] = int(lot["lot_inventory_id"]) if lot else None
                    subtotal = round(sum(i["subtotal"] for i in items), 2)
                    sale_id = next_id(cur, "sales", "sale_id")
                    txn = f"IMP-{sale_day.strftime('%Y%m%d')}-{sale_id}"
                    created = datetime.combine(sale_day, datetime.min.time().replace(hour=12))
                    cur.execute(
                        """
                        INSERT INTO sales
                            (sale_id, transaction_id, customer_id, user_id, subtotal, discount_amount, tax_amount,
                             total_amount, cash_received, change_amount, payment_method,
                             points_redeemed, points_discount_value, status, date_created)
                        VALUES (%s, %s, NULL, %s, %s, 0, 0, %s, %s, 0, 'cash', 0, 0, 'completed', %s)
                        """,
                        (sale_id, txn, user_id, subtotal, subtotal, subtotal, created),
                    )
                    imported_sales += 1
                    for item in items:
                        item_id = next_id(cur, "sales_items", "id")
                        cur.execute(
                            """
                            INSERT INTO sales_items
                                (id, sale_id, drug_id, lot_id, quantity, price, subtotal, discount_amount, vat_exempt)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, 0, 0)
                            """,
                            (item_id, sale_id, item["drug_id"], item["lot_id"], item["qty"], item["price"], item["subtotal"]),
                        )
                        imported_items += 1
                if imported_items:
                    _log(
                        cur, request, "Upload Sales Data",
                        f"Imported {imported_items} line(s) across {imported_sales} sale(s) from {file.filename}.",
                    )
    except Exception as exc:
        return {"success": False, "message": "Could not save the uploaded sales. " + str(exc)}
    if imported_items == 0:
        return {"success": False, "message": "No data rows found. Check the header row and try again."}
    return {
        "success": True,
        "message": f"Imported {imported_items} line(s) in {imported_sales} sale(s). Inventory was not changed.",
        "sales": imported_sales,
        "items": imported_items,
    }



@router.get("/reports/export")
def export_report(request: Request, kind: str = "", format: str = "xlsx", period: int = 30, months: int = 6, start: str = "", end: str = ""):
    if not require_admin(request):
        return _unauthorized()
    fmt = "pdf" if str(format).lower() == "pdf" else "xlsx"
    kind = str(kind or "").lower().strip()
    if kind == "forecast":
        data = forecast(request, period=period)
        if hasattr(data, "body"):
            return data
        if data.get("insufficient_data"):
            headers = ["Note"]
            rows = [[data.get("message") or "Not enough sales history."]]
        else:
            headers = ["Date / item", "Predicted sales (PHP)", "Predicted qty"]
            rows = []
            labels = (data.get("forecast") or {}).get("labels") or []
            values = (data.get("forecast") or {}).get("values") or []
            for lab, val in zip(labels, values):
                rows.append([lab, val, ""])
            rows.append(["Predicted total sales", data.get("predicted_total_sales"), data.get("predicted_items_sold")])
            rows.append(["Top category", data.get("top_category"), ""])
            top = data.get("top_items") or {}
            for lab, qty in zip(top.get("labels") or [], top.get("data") or []):
                rows.append([lab, "", qty])
        title = f"Sales Forecast ({data.get('period') or period} days)"
        filename = f"forecast-{period}d"
    elif kind == "consumption":
        data = monthly_consumption(request, months=months, start=start, end=end)
        month_labs = data.get("months") or []
        headers = ["Medicine", "Brand", "Type"] + month_labs + ["Total"]
        rows = []
        for r in data.get("rows") or []:
            monthly = r.get("monthly") or {}
            rows.append(
                [r.get("generic_name"), r.get("brand_name"), r.get("category")]
                + [monthly.get(m, 0) for m in month_labs]
                + [r.get("total", 0)]
            )
        totals = data.get("monthly_totals") or {}
        rows.append(["TOTAL", "", ""] + [totals.get(m, 0) for m in month_labs] + [sum(totals.values())])
        title = f"Pieces sold each month ({len(month_labs)} months)"
        filename = f"consumption-{len(month_labs)}mo"
    elif kind == "procurement":
        data = procurement(request)
        headers = ["Section", "Name", "Cost (PHP)", "Pieces", "Notes"]
        rows = [["Summary", "Total cost", (data.get("summary") or {}).get("total_inventory_cost"), (data.get("summary") or {}).get("total_units"), ""]]
        for r in data.get("by_supplier") or []:
            rows.append(["Supplier", r.get("supplier_name"), r.get("total_cost"), r.get("total_units"), f"{r.get('distinct_drugs')} medicines"])
        for r in data.get("by_category") or []:
            rows.append(["Type", r.get("category"), r.get("total_cost"), r.get("total_units"), ""])
        title = "Cost of stock on hand"
        filename = "procurement"
    else:
        return {"success": False, "message": "Unknown report type."}

    kind_label = {"forecast": "Sales forecast", "consumption": "Monthly consumption", "procurement": "Procurement cost"}.get(kind, title)
    log_event("Generate Report", f"Generated the {kind_label} report as {fmt.upper()}.", request=request)

    if fmt == "pdf":
        return _file_response(pdf_bytes(title, headers, rows), f"{filename}.pdf", "pdf")
    sheets = [(title[:31], headers, rows)]
    return _file_response(workbook_bytes(title, sheets), f"{filename}.xlsx", "xlsx")

