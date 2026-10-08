"""Server-side automation: schema, expiry, alerts, trend/consignment POs, scheduler."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from psycopg2.extras import RealDictCursor

from app.activity import write_activity_log
from app.db import get_conn, next_id
from app.mailer import send_mail
from app.stock import sync_stock_status_for_drug

logger = logging.getLogger("pharmalink.automation")
MANILA = ZoneInfo("Asia/Manila")

LEAD_DAYS = 7
TARGET_DAYS = 30
OVERSTOCK_DAYS = 90
SALES_WINDOW_DAYS = 30

_scheduler = None
_last_run: dict = {}


def ensure_automation_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            ALTER TABLE drugs_master
            ADD COLUMN IF NOT EXISTS procurement_type VARCHAR(20) NOT NULL DEFAULT 'purchase'
            """
        )
        cur.execute(
            """
            ALTER TABLE drugs_master
            ADD COLUMN IF NOT EXISTS barcode VARCHAR(64)
            """
        )
        cur.execute(
            """
            ALTER TABLE drugs_master
            ADD COLUMN IF NOT EXISTS cost_price NUMERIC(12,4)
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_drugs_master_barcode
            ON drugs_master (barcode)
            WHERE barcode IS NOT NULL AND btrim(barcode) <> ''
            """
        )
        cur.execute(
            """
            UPDATE drugs_master
            SET barcode = 'PLK' || lpad(drug_id::text, 6, '0')
            WHERE barcode IS NULL OR btrim(barcode) = ''
            """
        )
        cur.execute(
            """
            ALTER TABLE customer_orders
            ADD COLUMN IF NOT EXISTS payment_method VARCHAR(20)
            """
        )
        cur.execute(
            """
            ALTER TABLE customer_orders
            ADD COLUMN IF NOT EXISTS payment_status VARCHAR(20) NOT NULL DEFAULT 'unpaid'
            """
        )
        cur.execute(
            """
            ALTER TABLE customer_orders
            ADD COLUMN IF NOT EXISTS payment_reference VARCHAR(120)
            """
        )
        cur.execute(
            """
            ALTER TABLE sales
            ADD COLUMN IF NOT EXISTS online_order_id INTEGER
            """
        )
        cur.execute(
            """
            UPDATE sales s
            SET online_order_id = co.order_id,
                payment_method = CASE
                    WHEN LOWER(COALESCE(co.payment_method, '')) IN ('gcash', 'maya', 'cash')
                    THEN LOWER(co.payment_method)
                    ELSE s.payment_method
                END
            FROM customer_orders co
            WHERE s.online_order_id IS NULL
              AND s.customer_id = co.customer_id
              AND LOWER(TRIM(co.order_status)) = 'completed'
              AND ABS(s.total_amount - co.total_amount) < 0.05
              AND s.date_created >= co.order_date - INTERVAL '1 minute'
              AND s.date_created <= COALESCE(co.updated_at, co.order_date) + INTERVAL '2 days'
              AND NOT EXISTS (
                    SELECT 1 FROM sales s2 WHERE s2.online_order_id = co.order_id
              )
              AND NOT EXISTS (
                    SELECT 1
                    FROM customer_orders other
                    WHERE other.customer_id = co.customer_id
                      AND other.order_id <> co.order_id
                      AND LOWER(TRIM(other.order_status)) = 'completed'
                      AND ABS(other.total_amount - co.total_amount) < 0.05
                      AND s.date_created >= other.order_date - INTERVAL '1 minute'
                      AND s.date_created <= COALESCE(other.updated_at, other.order_date) + INTERVAL '2 days'
              )
            """
        )
        cur.execute(
            """
            ALTER TABLE promos
            ADD COLUMN IF NOT EXISTS segment_label VARCHAR(80)
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS system_alerts (
                alert_id INTEGER PRIMARY KEY,
                alert_type VARCHAR(40) NOT NULL,
                severity VARCHAR(20) NOT NULL DEFAULT 'info',
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                ref_key VARCHAR(120) NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                email_sent_at TIMESTAMPTZ
            )
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_system_alerts_type_ref
            ON system_alerts (alert_type, ref_key)
            """
        )
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
        cur.execute(
            """
            ALTER TABLE suppliers
            ADD COLUMN IF NOT EXISTS consignment_policy VARCHAR(20) NOT NULL DEFAULT 'none'
            """
        )
        cur.execute(
            """
            ALTER TABLE inventory_lots
            ADD COLUMN IF NOT EXISTS return_status VARCHAR(20)
            """
        )
    conn.commit()


def _log_system(cur, action: str, details: str) -> None:
    log_id = next_id(cur, "activity_logs", "log_id")
    cur.execute(
        "INSERT INTO activity_logs (log_id, admin_name, action, details) VALUES (%s, %s, %s, %s)",
        (log_id, "System", action, details),
    )


def _upsert_alert(cur, alert_type: str, ref_key: str, severity: str, title: str, message: str) -> bool:
    cur.execute(
        "SELECT alert_id, message FROM system_alerts WHERE alert_type = %s AND ref_key = %s",
        (alert_type, ref_key),
    )
    existing = cur.fetchone()
    if existing:
        if existing.get("message") == message:
            return False
        cur.execute(
            """
            UPDATE system_alerts
            SET severity = %s, title = %s, message = %s, created_at = NOW(), email_sent_at = NULL
            WHERE alert_id = %s
            """,
            (severity, title, message, existing["alert_id"]),
        )
        _push_staff_alert(alert_type, title, message)
        return True
    alert_id = next_id(cur, "system_alerts", "alert_id")
    cur.execute(
        """
        INSERT INTO system_alerts (alert_id, alert_type, severity, title, message, ref_key)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (alert_id, alert_type, severity, title, message, ref_key),
    )
    _push_staff_alert(alert_type, title, message)
    return True


_PUSH_TITLES = {
    "expired": "Expired lot",
    "expiring": "Near expiry",
    "expiring_30": "Near expiry (30 days)",
    "expiring_90": "Near expiry (90 days)",
    "low": "Low stock",
    "low_stock": "Low stock",
    "out": "Out of stock",
    "out_of_stock": "Out of stock",
    "auto_po": "Automatic purchase order",
    "online_order": "New online order",
    "order_paid": "Payment received",
}


def _push_staff_alert(alert_type: str, title: str, message: str) -> None:
    friendly = _PUSH_TITLES.get((alert_type or "").lower())
    shown = friendly or title or "PharmaLink"
    try:
        from app.push import queue_staff_push
        queue_staff_push(shown, message, alert_type)
    except Exception:
        pass


def sync_all_stock_status(cur) -> None:
    cur.execute("SELECT drug_id FROM drugs_master WHERE is_active = 1")
    for row in cur.fetchall():
        sync_stock_status_for_drug(cur, int(row["drug_id"]))


def deactivate_expired_lots(cur) -> int:
    """Stop selling expired lots without changing remaining quantity or price."""
    cur.execute(
        """
        SELECT lot_inventory_id, lot_number, drug_id, current_stock, expiration_date
        FROM inventory_lots
        WHERE is_active = 1
          AND expiration_date < CURRENT_DATE
        """
    )
    rows = cur.fetchall()
    affected_drugs = set()
    for row in rows:
        lot_id = int(row["lot_inventory_id"])
        cur.execute("UPDATE inventory_lots SET is_active = 0 WHERE lot_inventory_id = %s", (lot_id,))
        affected_drugs.add(int(row["drug_id"]))
        name_row = {"generic_name": "", "brand_name": ""}
        cur.execute("SELECT generic_name, brand_name FROM drugs_master WHERE drug_id = %s", (row["drug_id"],))
        name_row = cur.fetchone() or name_row
        qty = int(row["current_stock"] or 0)
        exp = row["expiration_date"]
        exp_s = exp.isoformat() if hasattr(exp, "isoformat") else str(exp)
        _upsert_alert(
            cur,
            "expired",
            f"lot:{lot_id}",
            "critical",
            "Expired lot deactivated",
            f"Lot {row['lot_number']} ({name_row.get('generic_name')} / {name_row.get('brand_name')}) expired on {exp_s}. "
            f"Deactivated for sale; on-hand qty {qty} was not changed.",
        )
    for drug_id in affected_drugs:
        sync_stock_status_for_drug(cur, drug_id)
    if rows:
        _log_system(cur, "Auto Expiry", f"Deactivated {len(rows)} expired lot(s); stock quantities left unchanged.")
    return len(rows)


def scan_near_expiry(cur) -> int:
    created = 0
    cur.execute(
        """
        SELECT l.lot_inventory_id, l.lot_number, l.expiration_date, l.current_stock,
               d.generic_name, d.brand_name,
               (l.expiration_date - CURRENT_DATE) AS days_left
        FROM inventory_lots l
        JOIN drugs_master d ON d.drug_id = l.drug_id
        WHERE l.is_active = 1
          AND l.current_stock > 0
          AND l.expiration_date >= CURRENT_DATE
          AND l.expiration_date <= (CURRENT_DATE + INTERVAL '90 days')
        ORDER BY l.expiration_date ASC
        """
    )
    for row in cur.fetchall():
        days = int(row["days_left"] or 0)
        lot_id = int(row["lot_inventory_id"])
        if days <= 30:
            alert_type, severity, window = "expiring_30", "warning", "30"
        else:
            alert_type, severity, window = "expiring_90", "info", "90"
        title = f"Near expiry ({window} days)"
        message = (
            f"{row['generic_name']} ({row['brand_name']}), lot {row['lot_number']} - "
            f"{days} day(s) left, {int(row['current_stock'] or 0)} unit(s) on hand."
        )
        if _upsert_alert(cur, alert_type, f"lot:{lot_id}:{window}", severity, title, message):
            created += 1
    return created


def scan_stock_alerts(cur) -> int:
    created = 0
    cur.execute(
        """
        SELECT dm.drug_id, dm.generic_name, dm.brand_name, dm.minimum_stock, dm.stock_status,
               COALESCE((
                   SELECT SUM(current_stock) FROM inventory_lots
                   WHERE is_active = 1 AND expiration_date >= CURRENT_DATE AND drug_id = dm.drug_id
               ), 0) AS on_hand
        FROM drugs_master dm
        WHERE dm.is_active = 1 AND dm.stock_status IN ('low', 'out')
        """
    )
    for row in cur.fetchall():
        status = row["stock_status"]
        on_hand = int(row["on_hand"] or 0)
        if status == "out":
            msg = f"Out of stock: {row['generic_name']} ({row['brand_name']}) - restock immediately."
            severity = "critical"
        else:
            msg = (
                f"Low stock: {row['generic_name']} ({row['brand_name']}) - {on_hand} left "
                f"(min {int(row['minimum_stock'] or 0)})."
            )
            severity = "warning"
        title = "Out of stock" if status == "out" else "Low stock"
        if _upsert_alert(cur, status, f"drug:{row['drug_id']}", severity, title, msg):
            created += 1
    return created


def _prophet_units_by_drug(period: int = TARGET_DAYS) -> tuple[dict, str]:
    """Predicted units per drug for the next `period` days, from Facebook Prophet.

    Returns ({drug_id: units}, engine). Engine is "prophet" when the model ran,
    or "recent_average" when Prophet is unavailable. Drugs with fewer than two
    sale days are omitted so the caller can fall back per medicine.
    """
    history_days = 1095
    per_drug = {}
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT drug_id, sale_date, SUM(daily_qty) AS daily_qty
                FROM (
                    SELECT si.drug_id, s.date_created::date AS sale_date, SUM(si.quantity) AS daily_qty
                    FROM sales_items si
                    JOIN sales s ON si.sale_id = s.sale_id
                    WHERE LOWER(TRIM(s.status)) = 'completed'
                      AND s.date_created::date >= (CURRENT_DATE - %s)
                    GROUP BY si.drug_id, s.date_created::date
                    UNION ALL
                    SELECT od.drug_id, co.order_date::date AS sale_date, SUM(od.quantity) AS daily_qty
                    FROM order_details od
                    JOIN customer_orders co ON od.order_id = co.order_id
                    WHERE LOWER(TRIM(co.order_status)) IN ('completed', 'ready for pickup')
                      AND co.order_date::date >= (CURRENT_DATE - %s)
                    GROUP BY od.drug_id, co.order_date::date
                ) t
                GROUP BY drug_id, sale_date
                ORDER BY drug_id, sale_date ASC
                """,
                (history_days, history_days),
            )
            for row in cur.fetchall():
                did = int(row["drug_id"])
                bucket = per_drug.setdefault(did, [])
                sold_on = row["sale_date"]
                bucket.append({
                    "date": sold_on.isoformat() if hasattr(sold_on, "isoformat") else str(sold_on)[:10],
                    "qty": int(row["daily_qty"] or 0),
                })
    if not per_drug:
        return {}, "recent_average"
    today = datetime.now(MANILA).date()
    first_sale = min(date.fromisoformat(pt["date"]) for series in per_drug.values() for pt in series)
    range_start = max(first_sale, today - timedelta(days=history_days))
    items = {str(did): {"series": series} for did, series in per_drug.items()}
    fingerprint = hashlib.md5(
        json.dumps({"period": period, "today": today.isoformat(), "items": items}, sort_keys=True).encode()
    ).hexdigest()
    cache_file = Path(__file__).resolve().parent.parent / "cache" / "prophet_reorder_cache.json"
    if cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("fingerprint") == fingerprint and isinstance(cached.get("demand"), dict):
                return {int(k): int(v) for k, v in cached["demand"].items()}, "prophet"
        except Exception:
            pass
    payload = json.dumps({
        "purpose": "reorder",
        "period": period,
        "today": today.isoformat(),
        "range_start": range_start.isoformat(),
        "daily_sales": [],
        "items": items,
    })
    script = Path(__file__).resolve().parent.parent / "python" / "forecast_prophet.py"
    decoded = None
    if script.exists():
        for cmd in (["python", str(script)], ["py", "-3", str(script)]):
            try:
                proc = subprocess.run(cmd, input=payload, capture_output=True, text=True, timeout=180)
                if proc.returncode != 0:
                    continue
                decoded = json.loads(proc.stdout or "{}")
                if decoded.get("success") and not decoded.get("insufficient_data"):
                    break
                decoded = None
            except Exception:
                decoded = None
                continue
    if not decoded:
        logger.warning("Prophet reorder forecast unavailable; using the recent sales average.")
        return {}, "recent_average"
    demand = {}
    for item in decoded.get("items") or []:
        if item.get("engine") == "prophet" and item.get("predicted_qty") is not None:
            demand[int(item["drug_id"])] = max(0, int(item["predicted_qty"]))
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps({"fingerprint": fingerprint, "demand": {str(k): v for k, v in demand.items()}}),
            encoding="utf-8",
        )
    except Exception:
        pass
    return demand, "prophet"


def compute_reorder_suggestions() -> dict:
    lead, target, overstock, window = LEAD_DAYS, TARGET_DAYS, OVERSTOCK_DAYS, SALES_WINDOW_DAYS
    forecast_units, demand_engine = _prophet_units_by_drug(target)
    rows = []
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT d.drug_id, d.generic_name, d.brand_name, d.dosage, d.form, d.category,
                       d.minimum_stock, COALESCE(d.procurement_type, 'purchase') AS procurement_type,
                       COALESCE(stock.on_hand, 0) AS current_stock, COALESCE(recent.sold, 0) AS units_sold_30d,
                       recent_supplier.supplier_id AS suggested_supplier_id,
                       recent_supplier.supplier_name AS suggested_supplier_name,
                       recent_supplier.consignment_policy AS supplier_consignment_policy,
                       COALESCE((
                           SELECT SUM(il.current_stock)
                           FROM inventory_lots il
                           WHERE il.drug_id = d.drug_id
                             AND il.supplier = recent_supplier.supplier_id
                             AND il.is_active = 1
                             AND COALESCE(il.current_stock, 0) > 0
                             AND il.expiration_date >= CURRENT_DATE
                             AND il.expiration_date <= CURRENT_DATE + {NEAR_EXPIRY_DAYS} * INTERVAL '1 day'
                       ), 0) AS soon_expiry_qty
                FROM drugs_master d
                LEFT JOIN (
                    SELECT drug_id, SUM(current_stock) AS on_hand
                    FROM inventory_lots
                    WHERE is_active = 1 AND expiration_date >= CURRENT_DATE
                    GROUP BY drug_id
                ) stock ON stock.drug_id = d.drug_id
                LEFT JOIN (
                    SELECT si.drug_id, SUM(si.quantity) AS sold
                    FROM sales_items si
                    JOIN sales s ON si.sale_id = s.sale_id
                    WHERE s.date_created >= (CURRENT_DATE - INTERVAL '{window} days')
                      AND s.status = 'completed'
                    GROUP BY si.drug_id
                ) recent ON recent.drug_id = d.drug_id
                LEFT JOIN LATERAL (
                    SELECT s.supplier_id, s.supplier_name, s.consignment_policy
                    FROM suppliers s
                    WHERE s.supplier_id = COALESCE(
                        (
                            SELECT il.supplier
                            FROM inventory_lots il
                            WHERE il.drug_id = d.drug_id AND il.supplier IS NOT NULL
                            ORDER BY il.date_added DESC NULLS LAST, il.lot_inventory_id DESC
                            LIMIT 1
                        ),
                        (
                            SELECT sd.supplier_id
                            FROM supplier_drugs sd
                            WHERE sd.drug_id = d.drug_id
                            ORDER BY sd.id DESC
                            LIMIT 1
                        )
                    )
                ) recent_supplier ON true
                WHERE d.is_active = 1
                """
            )
            rows = cur.fetchall()
    suggestions = []
    for row in rows:
        current = int(row["current_stock"] or 0)
        minimum = int(row["minimum_stock"] or 0)
        sold = int(row["units_sold_30d"] or 0)
        drug_id = int(row["drug_id"])
        recent_avg = sold / window
        predicted = forecast_units.get(drug_id)
        if predicted is not None:
            avg = predicted / target
            source = "prophet"
        else:
            avg = recent_avg
            source = "recent_average"
        has_demand = avg > 0
        days_of_stock = round(current / avg, 1) if has_demand else None
        reorder_point = (avg * lead) + minimum
        target_stock = (avg * target) + minimum
        procurement = str(row.get("procurement_type") or "purchase").lower()
        if procurement not in {"purchase", "consignment"}:
            procurement = "purchase"
        policy = normalize_consignment_policy(row.get("supplier_consignment_policy"))
        soon_expiry = int(row.get("soon_expiry_qty") or 0)
        stock_for_order = current
        policy_note = ""
        auto_po = True
        if procurement == "consignment":
            if policy == "returnable":
                stock_for_order = max(0, current - min(soon_expiry, current))
                policy_note = (
                    f" Returnable consignment: {soon_expiry} piece(s) expire within {NEAR_EXPIRY_DAYS} days and are not counted as stock to keep."
                    if soon_expiry
                    else " Returnable consignment: no near-expiry stock to return, so the order uses stock on hand."
                )
            elif policy == "non_returnable":
                policy_note = " Non-returnable consignment: ordered like a purchase because this stock cannot be returned."
            else:
                auto_po = False
                policy_note = " No consignment policy on the supplier, so this stays a manual purchase order."
        shown_rate = round(avg, 1) if avg >= 1 else round(avg, 2)
        rate_text = f"~{shown_rate}/day"
        forecast_n = int(round(predicted)) if predicted is not None else 0
        unit_word = "unit" if forecast_n == 1 else "units"
        if source == "prophet":
            signal = f"Prophet forecast: {rate_text} for the next {target} days (about {forecast_n} {unit_word})"
        elif sold > 0:
            signal = f"Recent {window}-day average {rate_text} (Prophet needs at least 2 sale days for this medicine)"
        else:
            signal = ""
        action, qty, reduce_qty, reasoning = "ok", 0, 0, ""
        below_minimum = minimum > 0 and stock_for_order <= minimum
        if below_minimum or (has_demand and stock_for_order <= reorder_point):
            action = "increase"
            qty = max(1, int(math.ceil(target_stock - stock_for_order)))
            counted = (
                f" Unexpired stock is {current}, and {stock_for_order} of that is counted after the returnable near-expiry stock."
                if stock_for_order != current
                else f" Unexpired stock is {current}."
            )
            reasoning = (
                f"{signal}.{counted} Order {qty} to cover that forecast and still keep the minimum of {minimum}."
                if has_demand
                else f"Stock counted for reorder is at or below minimum ({minimum}); no forecasted demand - minimum-buffer refill."
            )
        elif has_demand and days_of_stock is not None and days_of_stock > overstock:
            action = "decrease"
            qty = 0
            reduce_qty = max(0, current - max(minimum, int(math.ceil(target_stock))))
            reasoning = (
                f"{signal}. This covers ~{days_of_stock} days - pause reorders"
                + (f" (about {reduce_qty} units above the {target}-day target)." if reduce_qty else ".")
            )
        elif not has_demand and current > minimum * 3 and minimum > 0:
            action = "decrease"
            qty = 0
            reduce_qty = max(0, current - minimum)
            reasoning = (
                f"No sales in {window} days, and stock ({current}) is well above minimum ({minimum})"
                + (f" - {reduce_qty} units can wait." if reduce_qty else ".")
            )
        else:
            reasoning = (
                f"{signal}. {days_of_stock} days of stock - sufficient."
                if has_demand
                else "No recent sales, and stock is within a reasonable range of the minimum."
            )
        if policy_note:
            reasoning = f"{reasoning}{policy_note}"
        suggestions.append({
            "drug_id": int(row["drug_id"]),
            "generic_name": row["generic_name"],
            "brand_name": row["brand_name"],
            "dosage": row["dosage"],
            "form": row["form"],
            "category": row["category"],
            "procurement_type": procurement,
            "consignment_policy": policy if procurement == "consignment" else "none",
            "auto_po": auto_po,
            "soon_expiry_qty": soon_expiry if procurement == "consignment" and policy == "returnable" else 0,
            "current_stock": current,
            "minimum_stock": minimum,
            "avg_daily_sales": round(avg, 2),
            "forecast_units": int(round(predicted)) if predicted is not None else None,
            "demand_source": source,
            "days_of_stock": days_of_stock,
            "action": action,
            "suggested_qty": qty,
            "suggested_reduce_qty": reduce_qty,
            "reasoning": reasoning,
            "suggested_supplier_id": int(row["suggested_supplier_id"]) if row["suggested_supplier_id"] is not None else None,
            "suggested_supplier_name": row["suggested_supplier_name"],
        })
    order = {"increase": 0, "decrease": 1, "ok": 2}
    suggestions.sort(
        key=lambda s: (
            order[s["action"]],
            s["days_of_stock"] if s["action"] == "increase" and s["days_of_stock"] is not None else 10**9,
        )
    )
    return {
        "generated_at": datetime.now(MANILA).isoformat(),
        "assumptions": {
            "lead_time_days": lead,
            "target_coverage_days": target,
            "overstock_days": overstock,
            "sales_window_days": window,
            "demand_model": demand_engine,
            "unexpired_stock_only": True,
            "consignment": "Returnable near-expiry stock is left out of the order qty. Non-returnable consignment is ordered like a purchase. A consignment supplier with no policy stays manual.",
        },
        "suggestions": suggestions,
    }


def create_purchase_orders_from_needs(needs, created_by: str, notes: str, raise_alerts: bool = False) -> dict:
    """Create one Pending PO per supplier from reorder-style need rows."""
    created = []
    skipped = []
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT poi.drug_id
                FROM purchase_order_items poi
                JOIN purchase_orders po ON po.po_id = poi.po_id
                WHERE po.status IN ('Pending', 'Partially Received')
                  AND poi.quantity_ordered > COALESCE(poi.quantity_received, 0)
                """
            )
            open_drugs = {int(r["drug_id"]) for r in cur.fetchall()}
            by_supplier = {}
            for item in needs:
                label = item.get("generic_name") or f"Drug #{item.get('drug_id')}"
                if item["drug_id"] in open_drugs:
                    skipped.append({"name": label, "reason": "already on an open PO"})
                    continue
                sid = item.get("suggested_supplier_id")
                if not sid:
                    skipped.append({"name": label, "reason": "no suggested supplier"})
                    continue
                by_supplier.setdefault(int(sid), []).append(item)
            order_date = datetime.now(MANILA).date()
            expected = (order_date + timedelta(days=LEAD_DAYS)).isoformat()
            for supplier_id, items in by_supplier.items():
                cur.execute(
                    "SELECT supplier_id, supplier_name FROM suppliers WHERE supplier_id = %s AND status = 'Active'",
                    (supplier_id,),
                )
                supplier = cur.fetchone()
                if not supplier:
                    for item in items:
                        skipped.append({
                            "name": item.get("generic_name") or f"Drug #{item.get('drug_id')}",
                            "reason": "supplier is inactive",
                        })
                    continue
                po_id = next_id(cur, "purchase_orders", "po_id")
                cur.execute(
                    """
                    INSERT INTO purchase_orders
                        (po_id, supplier_id, status, order_date, expected_date, notes, created_by)
                    VALUES (%s, %s, 'Pending', %s, %s, %s, %s)
                    """,
                    (po_id, supplier_id, order_date.isoformat(), expected, notes, created_by),
                )
                for item in items:
                    item_id = next_id(cur, "purchase_order_items", "po_item_id")
                    cur.execute(
                        """
                        INSERT INTO purchase_order_items (po_item_id, po_id, drug_id, quantity_ordered, unit_cost)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (item_id, po_id, item["drug_id"], item["suggested_qty"], None),
                    )
                po_number = f"PO-{po_id:05d}"
                action = "Auto Purchase Order" if raise_alerts else "Create Purchase Order"
                write_activity_log(
                    cur,
                    action,
                    f"Created {po_number} for {supplier['supplier_name']} with {len(items)} item(s).",
                    actor=created_by,
                )
                if raise_alerts:
                    _upsert_alert(
                        cur,
                        "auto_po",
                        f"po:{po_id}",
                        "info",
                        "Automatic purchase order",
                        f"{po_number} drafted for {supplier['supplier_name']} ({len(items)} item(s)). Confirm in Purchasing.",
                    )
                created.append({
                    "po_id": po_id,
                    "po_number": po_number,
                    "supplier_name": supplier["supplier_name"],
                    "item_count": len(items),
                })
    return {"created": created, "skipped": skipped}


def create_auto_purchase_orders() -> dict:
    data = compute_reorder_suggestions()
    needs = [
        s
        for s in data["suggestions"]
        if s["action"] == "increase"
        and s["suggested_qty"] > 0
        and s.get("auto_po", True)
    ]
    notes = (
        "AUTO: generated from the Facebook Prophet demand forecast for the next 30 days. "
        "Consignment medicines are included when the supplier is returnable or non-returnable. "
        "Returnable near-expiry stock is not counted as stock to keep. "
        "Review quantities before sending to the supplier."
    )
    result = create_purchase_orders_from_needs(needs, "System", notes, raise_alerts=True)
    return {"created": result["created"], "skipped": len(result["skipped"]), "needed": len(needs)}


def _admin_emails() -> list[str]:
    rows = []
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT DISTINCT s.email
                FROM staff_info s
                JOIN users u ON u.user_id = s.user_id
                JOIN role r ON r.role_id = u.role_id
                WHERE u.is_active = 1
                  AND LOWER(r.role_name) = 'admin'
                  AND s.email IS NOT NULL AND TRIM(s.email) <> ''
                """
            )
            rows = cur.fetchall()
    emails = []
    for row in rows:
        email = str(row.get("email") or "").strip()
        if email and email not in emails:
            emails.append(email)
    return emails


_STOCK_EMAIL_TYPES = (
    "expired",
    "expiring",
    "expiring_30",
    "expiring_90",
    "low",
    "low_stock",
    "out",
    "out_of_stock",
)


def email_alert_digest(stock_and_expiry_only: bool = False) -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            sql = """
                SELECT alert_id, alert_type, severity, title, message, created_at
                FROM system_alerts
                WHERE email_sent_at IS NULL
                  AND created_at >= (NOW() - INTERVAL '2 days')
            """
            params: tuple = ()
            if stock_and_expiry_only:
                sql += " AND alert_type = ANY(%s)"
                params = (list(_STOCK_EMAIL_TYPES),)
            sql += """
                ORDER BY
                    CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                    created_at DESC
                LIMIT 80
            """
            cur.execute(sql, params)
            pending = cur.fetchall()
    if not pending:
        return {"sent": 0, "alerts": 0, "to": _admin_emails()}
    emails = _admin_emails()
    if not emails:
        return {"sent": 0, "alerts": len(pending), "reason": "no admin email", "to": []}
    items = "".join(
        f"<li><strong>{row['title']}</strong> - {row['message']}</li>" for row in pending
    )
    if stock_and_expiry_only:
        intro = "These medicines need attention because stock is low or a lot is near expiry."
        subject = "PharmaLink: low stock and expiry"
    else:
        intro = "PharmaLink ran automated checks (stock, expiry, purchase orders)."
        subject = "PharmaLink automation alerts"
    html = (
        f"<p>{intro}</p>"
        f"<ul>{items}</ul>"
        "<p>Open the Admin portal to review inventory and purchase orders.</p>"
    )
    sent = 0
    last_error = None
    for email in emails:
        result = send_mail(email, subject, html)
        if result.get("success"):
            sent += 1
        else:
            last_error = result.get("message")
    if sent:
        ids = [int(r["alert_id"]) for r in pending]
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE system_alerts SET email_sent_at = NOW() WHERE alert_id = ANY(%s)",
                    (ids,),
                )
    outcome = {"sent": sent, "alerts": len(pending), "error": last_error, "to": emails}
    _last_run["email"] = {"at": datetime.now(MANILA).isoformat(), **outcome}
    return outcome


def run_hourly_jobs() -> dict:
    result = {"expired_lots": 0, "near_expiry_alerts": 0, "stock_alerts": 0}
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            sync_all_stock_status(cur)
            result["expired_lots"] = deactivate_expired_lots(cur)
            result["near_expiry_alerts"] = scan_near_expiry(cur)
            result["stock_alerts"] = scan_stock_alerts(cur)
    result["email"] = email_alert_digest(stock_and_expiry_only=True)
    _last_run["hourly"] = {"at": datetime.now(MANILA).isoformat(), **result}
    logger.info("hourly jobs: %s", result)
    return result


def run_daily_jobs() -> dict:
    hourly = run_hourly_jobs()
    pos = create_auto_purchase_orders()
    mail = email_alert_digest()
    result = {"hourly": hourly, "purchase_orders": pos, "email": mail}
    _last_run["daily"] = {"at": datetime.now(MANILA).isoformat(), **result}
    logger.info("daily jobs: %s", json.dumps(result, default=str))
    return result


def apply_received_lot(cur, drug_id: int, lot_number: str, expiration_date, qty: int, price, cost_price, po_id: int):
    """Add received qty. Never overwrite an existing lot's selling price or replace its stock."""
    from app.pricing import resolve_selling_price

    cur.execute(
        """
        SELECT lot_inventory_id, price, cost_price, current_stock, is_active, expiration_date
        FROM inventory_lots
        WHERE drug_id = %s AND lot_number = %s
        FOR UPDATE
        """,
        (drug_id, lot_number),
    )
    existing = cur.fetchone()
    if existing:
        lot_id = int(existing["lot_inventory_id"])
        new_stock = int(existing["current_stock"] or 0) + qty
        keep_price = float(existing["price"] or 0)
        if keep_price <= 0:
            keep_price = resolve_selling_price(cur, drug_id, cost_price if cost_price is not None else existing.get("cost_price"))
        new_cost = cost_price if cost_price is not None else existing.get("cost_price")
        cur.execute(
            """
            UPDATE inventory_lots
            SET current_stock = %s,
                cost_price = COALESCE(%s, cost_price),
                price = %s,
                is_active = CASE WHEN expiration_date >= CURRENT_DATE THEN 1 ELSE is_active END
            WHERE lot_inventory_id = %s
            """,
            (new_stock, new_cost, keep_price, lot_id),
        )
        return lot_id, "updated"
    sell = resolve_selling_price(cur, drug_id, cost_price)
    lot_id = next_id(cur, "inventory_lots", "lot_inventory_id")
    cur.execute(
        """
        INSERT INTO inventory_lots
            (lot_inventory_id, drug_id, lot_number, expiration_date, current_stock, price, cost_price, supplier, is_active, date_added)
        VALUES (%s, %s, %s, %s, %s, %s, %s, (SELECT supplier_id FROM purchase_orders WHERE po_id = %s), 1, CURRENT_TIMESTAMP)
        """,
        (lot_id, drug_id, lot_number, expiration_date, qty, sell, cost_price, po_id),
    )
    return lot_id, "created"


NEAR_EXPIRY_DAYS = 90
CONSIGNMENT_POLICIES = {"none", "returnable", "non_returnable"}


def normalize_consignment_policy(value) -> str:
    raw = str(value or "none").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in {"nonreturnable", "non_returnable", "not_returnable"}:
        return "non_returnable"
    if raw == "returnable":
        return "returnable"
    return "none"


def run_consignment_cross_check() -> dict:
    """Mark near-expiry lots returnable/non-returnable from the supplier policy. Manual only."""
    marked_returnable = 0
    marked_non_returnable = 0
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT l.lot_inventory_id, s.consignment_policy
                FROM inventory_lots l
                JOIN suppliers s ON l.supplier = s.supplier_id
                WHERE l.is_active = 1
                  AND COALESCE(l.current_stock, 0) > 0
                  AND l.expiration_date >= CURRENT_DATE
                  AND l.expiration_date <= CURRENT_DATE + %s * INTERVAL '1 day'
                  AND LOWER(COALESCE(s.consignment_policy, 'none')) IN ('returnable', 'non_returnable')
                """,
                (NEAR_EXPIRY_DAYS,),
            )
            rows = cur.fetchall()
            for row in rows:
                policy = normalize_consignment_policy(row.get("consignment_policy"))
                if policy not in {"returnable", "non_returnable"}:
                    continue
                cur.execute(
                    "UPDATE inventory_lots SET return_status = %s WHERE lot_inventory_id = %s",
                    (policy, int(row["lot_inventory_id"])),
                )
                if policy == "returnable":
                    marked_returnable += 1
                else:
                    marked_non_returnable += 1
            total = marked_returnable + marked_non_returnable
    return {
        "success": True,
        "updated": marked_returnable + marked_non_returnable,
        "returnable": marked_returnable,
        "non_returnable": marked_non_returnable,
        "window_days": NEAR_EXPIRY_DAYS,
    }


def alert_email_status() -> dict:
    recipients = _admin_emails()
    last_sent_at = None
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT MAX(email_sent_at) AS last_sent_at
                FROM system_alerts
                WHERE email_sent_at IS NOT NULL
                  AND alert_type = ANY(%s)
                """,
                (list(_STOCK_EMAIL_TYPES),),
            )
            row = cur.fetchone()
            if row and row.get("last_sent_at"):
                last_sent_at = row["last_sent_at"].isoformat()
    return {
        "recipients": recipients,
        "last_sent_at": last_sent_at,
        "last_result": _last_run.get("email") or None,
    }


def last_run_status() -> dict:
    return {
        "scheduler": "running" if _scheduler and _scheduler.running else "stopped",
        "timezone": "Asia/Manila",
        "last_run": _last_run,
        "alert_email": alert_email_status(),
    }


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    scheduler = BackgroundScheduler(timezone=MANILA)
    scheduler.add_job(
        run_hourly_jobs,
        IntervalTrigger(hours=1),
        id="pharmalink-hourly",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_daily_jobs,
        CronTrigger(hour=6, minute=0, timezone=MANILA),
        id="pharmalink-daily",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    scheduler.add_job(run_hourly_jobs, "date", run_date=datetime.now(MANILA) + timedelta(seconds=12), id="pharmalink-startup")
    logger.info("automation scheduler started")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
