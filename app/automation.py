"""Server-side automation: schema, expiry, alerts, trend/consignment POs, scheduler."""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import logging
import math
import os
import subprocess
import sys
import threading
from datetime import date, datetime, timedelta
from html import escape as _esc
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
# These environment values seed the initial schedule; admins can change it in Purchase Orders.
AUTO_PO_DAY = (os.getenv("AUTO_PO_DAY", "mon") or "mon").strip().lower()
try:
    AUTO_PO_HOUR = max(0, min(23, int(os.getenv("AUTO_PO_HOUR", "8"))))
except ValueError:
    AUTO_PO_HOUR = 8
PO_SCHEDULE_KEY = "purchase_order_schedule"
PHARMACY_NAME = (os.getenv("PHARMACY_NAME", "PharmaLink") or "PharmaLink").strip()
PHARMACY_CONTACT = (os.getenv("PHARMACY_CONTACT", "") or "").strip()
TARGET_DAYS = 30
OVERSTOCK_DAYS = 90
SALES_WINDOW_DAYS = 30
MAX_ORDER_UNITS = 200

_scheduler = None
_last_run: dict = {}
_restock_lock = threading.Lock()
_restock_timer_lock = threading.Lock()
_pending_drug_ids: set[int] = set()
_pending_all_drugs = False
_restock_task_scheduled = False


def _default_po_schedule() -> dict:
    weekdays = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    return {
        "frequency": "weekly",
        "weekday": weekdays.get(AUTO_PO_DAY, 0),
        "day_of_month": 1,
        "hour": AUTO_PO_HOUR,
        "minute": 0,
    }


def _schedule_signature(schedule: dict) -> tuple:
    return (
        schedule["frequency"],
        schedule["weekday"],
        schedule["day_of_month"],
        schedule["hour"],
        schedule["minute"],
    )


def _schedule_time(schedule: dict, scheduled_date: date) -> datetime:
    return datetime(
        scheduled_date.year,
        scheduled_date.month,
        scheduled_date.day,
        schedule["hour"],
        schedule["minute"],
        tzinfo=MANILA,
    )


def _monthly_schedule_date(year: int, month: int, day_of_month: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day_of_month, last_day))


def _latest_due_schedule_date(schedule: dict, now: datetime | None = None) -> date:
    now = now or datetime.now(MANILA)
    today = now.date()
    frequency = schedule["frequency"]
    if frequency == "daily":
        candidate = today
        if _schedule_time(schedule, candidate) > now:
            candidate -= timedelta(days=1)
        return candidate
    if frequency == "weekly":
        days_back = (today.weekday() - schedule["weekday"]) % 7
        candidate = today - timedelta(days=days_back)
        if _schedule_time(schedule, candidate) > now:
            candidate -= timedelta(days=7)
        return candidate

    candidate = _monthly_schedule_date(today.year, today.month, schedule["day_of_month"])
    if _schedule_time(schedule, candidate) > now:
        previous_month = today.month - 1 or 12
        previous_year = today.year - (1 if today.month == 1 else 0)
        candidate = _monthly_schedule_date(previous_year, previous_month, schedule["day_of_month"])
    return candidate


def _next_schedule_datetime(schedule: dict, now: datetime | None = None) -> datetime:
    now = now or datetime.now(MANILA)
    today = now.date()
    frequency = schedule["frequency"]
    if frequency == "daily":
        candidate = _schedule_time(schedule, today)
        return candidate if candidate > now else candidate + timedelta(days=1)
    if frequency == "weekly":
        days_forward = (schedule["weekday"] - today.weekday()) % 7
        candidate = _schedule_time(schedule, today + timedelta(days=days_forward))
        return candidate if candidate > now else candidate + timedelta(days=7)

    candidate_date = _monthly_schedule_date(today.year, today.month, schedule["day_of_month"])
    candidate = _schedule_time(schedule, candidate_date)
    if candidate > now:
        return candidate
    next_month = today.month % 12 + 1
    next_year = today.year + (1 if today.month == 12 else 0)
    return _schedule_time(schedule, _monthly_schedule_date(next_year, next_month, schedule["day_of_month"]))


def _parse_po_schedule(raw: str | None) -> dict:
    try:
        value = json.loads(raw or "{}")
        if not isinstance(value, dict):
            raise ValueError("Choose a daily, weekly, or monthly PO schedule.")
        frequency = value.get("frequency", "weekly")
        if not isinstance(frequency, str) or frequency not in {"daily", "weekly", "monthly"}:
            raise ValueError("Choose a daily, weekly, or monthly PO schedule.")
        if frequency == "weekly" and "weekday" not in value:
            raise ValueError("Choose a weekday for the weekly PO schedule.")
        if frequency == "monthly" and "day_of_month" not in value:
            raise ValueError("Choose a day of the month for the monthly PO schedule.")
        weekday = int(value.get("weekday", 0))
        day_of_month = int(value.get("day_of_month", 1))
        hour = int(value.get("hour", 8))
        minute = int(value.get("minute", 0))
        if not 0 <= weekday <= 6:
            raise ValueError("Choose a valid weekday.")
        if not 1 <= day_of_month <= 31:
            raise ValueError("Choose a day of the month from 1 to 31.")
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Choose a valid Manila schedule time.")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Choose "):
            raise
        raise ValueError("The saved PO schedule is invalid. Set a new schedule in Purchase Orders.") from exc
    return {
        "frequency": frequency,
        "weekday": weekday,
        "day_of_month": day_of_month,
        "hour": hour,
        "minute": minute,
    }


def _read_po_schedule(cur) -> tuple[dict, str]:
    cur.execute("SELECT setting_value FROM app_settings WHERE setting_key = %s", (PO_SCHEDULE_KEY,))
    row = cur.fetchone()
    if row:
        raw = row["setting_value"] if isinstance(row, dict) else row[0]
        schedule = _parse_po_schedule(raw)
        try:
            last_run = json.loads(raw or "{}").get("last_run_occurrence") or ""
        except (TypeError, ValueError):
            last_run = ""
        return schedule, str(last_run)

    schedule = _default_po_schedule()
    initial = {**schedule, "last_run_occurrence": _latest_due_schedule_date(schedule).isoformat()}
    cur.execute(
        """
        INSERT INTO app_settings (setting_key, setting_value, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (setting_key) DO NOTHING
        """,
        (PO_SCHEDULE_KEY, json.dumps(initial)),
    )
    return schedule, initial["last_run_occurrence"]


def get_po_schedule() -> dict:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            schedule, _ = _read_po_schedule(cur)
    now = datetime.now(MANILA)
    result = {**schedule, "next_run_at": _next_schedule_datetime(schedule, now).isoformat()}
    result["weekday_name"] = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")[schedule["weekday"]]
    return result


def save_po_schedule(schedule_data: dict) -> dict:
    schedule = _parse_po_schedule(json.dumps(schedule_data or {}))
    now = datetime.now(MANILA)
    saved = {**schedule, "last_run_occurrence": _latest_due_schedule_date(schedule, now).isoformat()}
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (setting_key, setting_value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (setting_key) DO UPDATE
                SET setting_value = EXCLUDED.setting_value, updated_at = NOW()
                """,
                (PO_SCHEDULE_KEY, json.dumps(saved)),
            )
    return get_po_schedule()
def ensure_automation_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            DO $$
            BEGIN
                IF to_regclass('purchase_orders') IS NOT NULL THEN
                    ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS sent_to_supplier_at TIMESTAMPTZ;
                    ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS sent_to_supplier_by VARCHAR(100);
                    ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS send_error TEXT;
                    ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS send_on_schedule BOOLEAN NOT NULL DEFAULT FALSE;
                    ALTER TABLE purchase_orders ADD COLUMN IF NOT EXISTS send_started_at TIMESTAMPTZ;
                END IF;
            END $$;
            """
        )
        cur.execute(
            """
            ALTER TABLE prescriptions
            ADD COLUMN IF NOT EXISTS review_status VARCHAR(20) NOT NULL DEFAULT 'pending'
            """
        )
        cur.execute(
            """
            ALTER TABLE prescriptions
            ADD COLUMN IF NOT EXISTS id_filename VARCHAR(255)
            """
        )
        cur.execute(
            """
            ALTER TABLE prescriptions
            ADD COLUMN IF NOT EXISTS identity_verified BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        cur.execute(
            """
            ALTER TABLE prescriptions
            ADD COLUMN IF NOT EXISTS review_note TEXT
            """
        )
        cur.execute(
            """
            ALTER TABLE prescriptions
            ADD COLUMN IF NOT EXISTS reviewed_by INTEGER
            """
        )
        cur.execute(
            """
            ALTER TABLE prescriptions
            ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ
            """
        )
        cur.execute(
            """
            ALTER TABLE prescriptions
            ADD COLUMN IF NOT EXISTS reviewed_drug_ids INTEGER[] NOT NULL DEFAULT '{}'
            """
        )
        cur.execute(
            """
            ALTER TABLE order_details
            ADD COLUMN IF NOT EXISTS prescription_id INTEGER
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_order_details_prescription_id
            ON order_details (prescription_id)
            WHERE prescription_id IS NOT NULL
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_prescriptions_review_status
            ON prescriptions (review_status, created_at DESC)
            """
        )
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
            ALTER TABLE drugs_master
            ADD COLUMN IF NOT EXISTS maximum_stock INTEGER
            """
        )
        cur.execute(
            """
            ALTER TABLE drugs_master
            ADD COLUMN IF NOT EXISTS units_per_box INTEGER NOT NULL DEFAULT 1
            """
        )
        cur.execute(
            """
            UPDATE drugs_master
            SET maximum_stock = GREATEST(COALESCE(minimum_stock, 20) * 3, COALESCE(minimum_stock, 20) + 20)
            WHERE maximum_stock IS NULL OR maximum_stock <= COALESCE(minimum_stock, 0)
            """
        )
        cur.execute(
            """
            ALTER TABLE sales
            ADD COLUMN IF NOT EXISTS discount_id_type VARCHAR(20)
            """
        )
        cur.execute(
            """
            ALTER TABLE sales
            ADD COLUMN IF NOT EXISTS discount_id_number VARCHAR(80)
            """
        )
        cur.execute(
            """
            ALTER TABLE sales
            ADD COLUMN IF NOT EXISTS discount_id_holder VARCHAR(120)
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
            UPDATE customer_orders co
            SET payment_status = 'paid'
            WHERE LOWER(TRIM(COALESCE(co.order_status, ''))) IN ('completed', 'delivered')
              AND LOWER(TRIM(COALESCE(co.payment_status, 'unpaid'))) <> 'paid'
              AND EXISTS (
                    SELECT 1
                    FROM sales s
                    WHERE s.online_order_id = co.order_id
                      AND LOWER(TRIM(COALESCE(s.status, ''))) = 'completed'
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
        cur.execute(
            """
            DO $$
            DECLARE r record;
            BEGIN
                FOR r IN
                    SELECT con.conname
                    FROM pg_constraint con
                    JOIN pg_class rel ON rel.oid = con.conrelid
                    JOIN pg_namespace nsp ON nsp.oid = rel.relnamespace
                    WHERE nsp.nspname = 'public'
                      AND rel.relname = 'inventory_lots'
                      AND con.contype = 'u'
                      AND pg_get_constraintdef(con.oid) NOT ILIKE '%expiration_date%'
                LOOP
                    EXECUTE format('ALTER TABLE inventory_lots DROP CONSTRAINT %I', r.conname);
                END LOOP;
                FOR r IN
                    SELECT ic.relname AS index_name
                    FROM pg_index ix
                    JOIN pg_class tc ON tc.oid = ix.indrelid
                    JOIN pg_class ic ON ic.oid = ix.indexrelid
                    JOIN pg_namespace nsp ON nsp.oid = tc.relnamespace
                    WHERE nsp.nspname = 'public'
                      AND tc.relname = 'inventory_lots'
                      AND ix.indisunique
                      AND NOT ix.indisprimary
                      AND ic.relname <> 'uq_inventory_lots_lot_expiry'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM pg_attribute a
                          WHERE a.attrelid = tc.oid
                            AND a.attnum = ANY (ix.indkey)
                            AND a.attname = 'expiration_date'
                      )
                LOOP
                    EXECUTE format('DROP INDEX IF EXISTS %I', r.index_name);
                END LOOP;
            END $$;
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_inventory_lots_lot_expiry
            ON inventory_lots (drug_id, lot_number, expiration_date)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_lots_active_exp
            ON inventory_lots (is_active, expiration_date)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_inventory_lots_drug_active
            ON inventory_lots (drug_id, is_active)
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sales_completed_date
            ON sales (status, date_created)
            """
        )
        cur.execute(
            """
            ALTER TABLE drugs_master
            ADD COLUMN IF NOT EXISTS sale_class VARCHAR(10)
            """
        )
        from app.sale_class import classify_sale_class

        cur.execute(
            """
            SELECT drug_id, generic_name, dosage, category
            FROM drugs_master
            WHERE sale_class IS NULL OR btrim(sale_class) = ''
            """
        )
        for drug_id, generic_name, dosage, category in cur.fetchall():
            cur.execute(
                "UPDATE drugs_master SET sale_class = %s WHERE drug_id = %s",
                (classify_sale_class(generic_name, category, dosage), drug_id),
            )
        cur.execute("ALTER TABLE drugs_master ALTER COLUMN sale_class SET DEFAULT 'rx'")
        cur.execute(
            """
            UPDATE drugs_master
            SET sale_class = 'rx'
            WHERE sale_class IS NULL OR btrim(sale_class) = ''
            """
        )
        cur.execute("ALTER TABLE drugs_master ALTER COLUMN sale_class SET NOT NULL")
        cur.execute(
            """
            DO $$
            BEGIN
                ALTER TABLE drugs_master
                ADD CONSTRAINT drugs_master_sale_class_chk
                CHECK (sale_class IN ('otc', 'rx'));
            EXCEPTION
                WHEN duplicate_object THEN NULL;
            END $$;
            """
        )
    conn.commit()


def _log_system(cur, action: str, details: str) -> None:
    log_id = next_id(cur, "activity_logs", "log_id")
    cur.execute(
        "INSERT INTO activity_logs (log_id, admin_name, action, details) VALUES (%s, %s, %s, %s)",
        (log_id, "System", action, details),
    )


def _lot_phrase(lot_number) -> str:
    raw = str(lot_number or "").strip() or "unknown"
    if raw.upper().startswith("LOT"):
        return raw
    return f"lot {raw}"


def _upsert_alert(cur, alert_type: str, ref_key: str, severity: str, title: str, message: str) -> bool:
    cur.execute(
        "SELECT alert_id, message, severity, title FROM system_alerts WHERE alert_type = %s AND ref_key = %s",
        (alert_type, ref_key),
    )
    existing = cur.fetchone()
    if existing:
        if (
            existing.get("message") == message
            and existing.get("severity") == severity
            and existing.get("title") == title
        ):
            return False
        became_worse = existing.get("severity") != severity or existing.get("title") != title
        if became_worse:
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
        cur.execute(
            "UPDATE system_alerts SET message = %s WHERE alert_id = %s",
            (message, existing["alert_id"]),
        )
        return False
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
    active_refs = []
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
        qty = int(row["current_stock"] or 0)
        window = "30" if days <= 30 else "90"
        severity = "warning" if days <= 30 else "info"
        title = f"Near expiry ({window} days)"
        day_word = "day" if days == 1 else "days"
        piece_word = "piece" if qty == 1 else "pieces"
        message = (
            f"{row['generic_name']} ({row['brand_name']}), {_lot_phrase(row['lot_number'])} - "
            f"{days} {day_word} left, {qty} {piece_word} on hand."
        )
        ref = f"lot:{lot_id}"
        active_refs.append(ref)
        cur.execute(
            """
            SELECT alert_id, alert_type, ref_key, message, severity, title
            FROM system_alerts
            WHERE (alert_type = 'expiring' AND ref_key = %s)
               OR (
                    alert_type IN ('expiring', 'expiring_30', 'expiring_90')
                    AND ref_key IN (%s, %s, %s)
               )
            ORDER BY alert_id
            """,
            (ref, ref, f"lot:{lot_id}:30", f"lot:{lot_id}:90"),
        )
        siblings = cur.fetchall() or []
        keep = next(
            (row for row in siblings if row["alert_type"] == "expiring" and row["ref_key"] == ref),
            siblings[0] if siblings else None,
        )
        extra_ids = [int(row["alert_id"]) for row in siblings if keep is None or int(row["alert_id"]) != int(keep["alert_id"])]
        if extra_ids:
            cur.execute("DELETE FROM system_alerts WHERE alert_id = ANY(%s)", (extra_ids,))
        if keep:
            already = (
                keep.get("alert_type") == "expiring"
                and keep.get("ref_key") == ref
                and keep.get("message") == message
                and keep.get("severity") == severity
                and keep.get("title") == title
            )
            if already:
                continue
            window_changed = keep.get("title") != title or keep.get("severity") != severity
            if window_changed:
                cur.execute(
                    """
                    UPDATE system_alerts
                    SET alert_type = 'expiring', ref_key = %s, severity = %s, title = %s,
                        message = %s, created_at = NOW(), email_sent_at = NULL
                    WHERE alert_id = %s
                    """,
                    (ref, severity, title, message, keep["alert_id"]),
                )
                _push_staff_alert("expiring", title, message)
                created += 1
            else:
                cur.execute(
                    """
                    UPDATE system_alerts
                    SET alert_type = 'expiring', ref_key = %s, message = %s
                    WHERE alert_id = %s
                    """,
                    (ref, message, keep["alert_id"]),
                )
            continue
        if _upsert_alert(cur, "expiring", ref, severity, title, message):
            created += 1
    cur.execute("DELETE FROM system_alerts WHERE alert_type IN ('expiring_30', 'expiring_90')")
    if active_refs:
        cur.execute(
            """
            DELETE FROM system_alerts
            WHERE alert_type = 'expiring'
              AND ref_key LIKE 'lot:%%'
              AND NOT (ref_key = ANY(%s))
            """,
            (active_refs,),
        )
    else:
        cur.execute(
            "DELETE FROM system_alerts WHERE alert_type = 'expiring' AND ref_key LIKE 'lot:%%'"
        )
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
            piece_word = "piece" if on_hand == 1 else "pieces"
            msg = (
                f"Low stock: {row['generic_name']} ({row['brand_name']}) - {on_hand} {piece_word} left "
                f"(min {int(row['minimum_stock'] or 0)})."
            )
            severity = "warning"
        title = "Out of stock" if status == "out" else "Low stock"
        ref = f"drug:{row['drug_id']}"
        other = "out" if status == "low" else "low"
        cur.execute(
            "DELETE FROM system_alerts WHERE alert_type = %s AND ref_key = %s",
            (other, ref),
        )
        if _upsert_alert(cur, status, ref, severity, title, msg):
            created += 1
    return created


def _prophet_units_by_drug(
    period: int = TARGET_DAYS,
    *,
    allow_background_fit: bool = False,
) -> tuple[dict, str]:
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
                SELECT si.drug_id, s.date_created::date AS sale_date, SUM(si.quantity) AS daily_qty
                FROM sales_items si
                JOIN sales s ON si.sale_id = s.sale_id
                WHERE LOWER(TRIM(s.status)) = 'completed'
                  AND s.date_created::date >= (CURRENT_DATE - %s)
                GROUP BY si.drug_id, s.date_created::date
                ORDER BY si.drug_id, s.date_created::date ASC
                """,
                (history_days,),
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
            cached_demand = cached.get("demand")
            if isinstance(cached_demand, dict):
                if cached.get("fingerprint") == fingerprint:
                    return {int(k): int(v) for k, v in cached_demand.items()}, "prophet"
                generated_at = datetime.fromisoformat(str(cached.get("generated_at") or ""))
                if generated_at.date() == today:
                    logger.info("Using today's cached Prophet reorder forecast while it refreshes.")
                    return {int(k): int(v) for k, v in cached_demand.items()}, "prophet"
        except Exception:
            pass
    if not allow_background_fit:
        logger.warning("Prophet reorder forecast is not ready; using recent sales until the background refresh completes.")
        return {}, "recent_average"
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
        for cmd in ([sys.executable, str(script)], ["py", "-3", str(script)]):
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
            json.dumps({
                "fingerprint": fingerprint,
                "generated_at": datetime.now(MANILA).isoformat(),
                "demand": {str(k): v for k, v in demand.items()},
            }),
            encoding="utf-8",
        )
    except Exception:
        pass
    return demand, "prophet"


def refresh_prophet_reorder_forecast() -> None:
    """Refresh the reorder forecast away from customer-facing request workers."""
    try:
        demand, engine = _prophet_units_by_drug(TARGET_DAYS, allow_background_fit=True)
        if engine != "prophet":
            logger.warning("Background Prophet reorder forecast did not complete; suggestions will use recent averages.")
            return
        logger.info("Background Prophet reorder forecast refreshed for %s medicines.", len(demand))
    except Exception:
        logger.exception("Background Prophet reorder forecast refresh failed.")


def _default_maximum(minimum: int) -> int:
    minimum = max(1, int(minimum or 0))
    return max(minimum * 3, minimum + 20)


def _max_order_boxes(maximum_stock: int, current_stock: int, units_per_box: int) -> int:
    """Whole boxes allowed by the stock ceiling and the 200-piece order cap."""
    room = max(0, int(maximum_stock) - int(current_stock))
    pack_size = max(1, int(units_per_box or 1))
    return min(room, MAX_ORDER_UNITS) // pack_size


def _suggested_po_quantity(
    current_stock: int,
    minimum_stock: int,
    target_stock: float,
    has_demand: bool,
    units_per_box: int,
    max_order_qty: int,
) -> int:
    """Suggest whole boxes to reach forecast coverage, or minimum stock without demand."""
    target = target_stock if has_demand else minimum_stock
    shortfall = max(0, math.ceil(target - current_stock))
    pack_size = max(1, int(units_per_box or 1))
    requested = math.ceil(shortfall / pack_size) * pack_size
    return min(requested, max(0, int(max_order_qty)))


EOQ_ORDER_COST = 50.0
EOQ_HOLDING_RATE = 0.20


def _eoq_units(annual_demand: float, unit_cost: float) -> int:
    """Classic EOQ = sqrt(2DS/H). S is peso per order; H is 20% of unit cost per year."""
    demand = max(0.0, float(annual_demand or 0))
    if demand <= 0:
        return 0
    holding = max(float(unit_cost or 0) * EOQ_HOLDING_RATE, 1.0)
    return max(1, int(math.ceil(math.sqrt((2.0 * demand * EOQ_ORDER_COST) / holding))))


def compute_reorder_suggestions(use_forecast: bool = True) -> dict:
    lead, target, overstock, window = LEAD_DAYS, TARGET_DAYS, OVERSTOCK_DAYS, SALES_WINDOW_DAYS
    if use_forecast:
        forecast_units, demand_engine = _prophet_units_by_drug(target)
    else:
        forecast_units, demand_engine = {}, "recent_average"
    rows = []
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT d.drug_id, d.generic_name, d.brand_name, d.dosage, d.form, d.category,
                       d.minimum_stock, COALESCE(d.maximum_stock, 0) AS maximum_stock,
                       COALESCE(d.units_per_box, 1) AS units_per_box,
                       COALESCE(d.cost_price, 0) AS unit_cost,
                       COALESCE(d.procurement_type, 'purchase') AS procurement_type,
                       COALESCE(stock.on_hand, 0) AS current_stock, COALESCE(recent.sold, 0) AS units_sold_30d,
                       recent_supplier.supplier_id AS suggested_supplier_id,
                       recent_supplier.supplier_name AS suggested_supplier_name,
                       recent_supplier.email AS suggested_supplier_email,
                       recent_supplier.consignment_policy AS supplier_consignment_policy,
                       open_pos.po_numbers AS open_po_numbers,
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
                    SELECT STRING_AGG(
                        DISTINCT 'PO-' || LPAD(po.po_id::text, 5, '0'),
                        ', '
                    ) AS po_numbers
                    FROM purchase_order_items poi
                    JOIN purchase_orders po ON po.po_id = poi.po_id
                    WHERE poi.drug_id = d.drug_id
                      AND po.status IN ('Pending', 'Partially Received')
                      AND poi.quantity_ordered > COALESCE(poi.quantity_received, 0)
                ) open_pos ON true
                LEFT JOIN LATERAL (
                    SELECT s.supplier_id, s.supplier_name, s.email, s.consignment_policy
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
        stock_for_order = current
        auto_po = True
        shown_rate = round(avg, 1) if avg >= 1 else round(avg, 2)
        rate_text = f"~{shown_rate}/day"
        forecast_n = int(round(predicted)) if predicted is not None else 0
        unit_word = "unit" if forecast_n == 1 else "units"
        if source == "prophet":
            signal = f"Prophet forecast: {rate_text} for the next {target} days (about {forecast_n} {unit_word})"
        elif sold > 0:
            signal = f"Recent {window}-day average {rate_text} (Prophet forecast unavailable for this medicine)"
        else:
            signal = ""
        action, qty, reduce_qty, reasoning = "ok", 0, 0, ""
        maximum = int(row["maximum_stock"] or 0)
        if maximum <= minimum:
            maximum = _default_maximum(minimum)
        units_per_box = max(1, int(row.get("units_per_box") or 1))
        unit_cost = float(row.get("unit_cost") or 0)
        annual_demand = avg * 365.0 if has_demand else 0.0
        eoq = _eoq_units(annual_demand, unit_cost)
        room = max(0, maximum - stock_for_order)
        max_order_boxes = _max_order_boxes(maximum, stock_for_order, units_per_box)
        max_order_qty = max_order_boxes * units_per_box
        below_reorder_point = has_demand and stock_for_order <= reorder_point
        below_minimum = minimum > 0 and stock_for_order <= minimum
        needs_reorder = below_reorder_point or below_minimum
        at_or_above_max = maximum > 0 and stock_for_order >= maximum
        suggested_po_qty = 0
        if at_or_above_max:
            action = "decrease"
            qty = 0
            reduce_qty = max(0, stock_for_order - maximum)
            reasoning = (
                f"{signal}. Stock ({current}) is at or above the maximum ({maximum})"
                + (f" - {reduce_qty} units can wait." if reduce_qty else ".")
            )
        elif needs_reorder:
            action = "increase"
            requested_qty = eoq if eoq > 0 else room
            qty = min(max(0, int(requested_qty or 0)), max_order_qty)
            qty = (qty // units_per_box) * units_per_box
            suggested_po_qty = _suggested_po_quantity(
                stock_for_order,
                minimum,
                target_stock,
                has_demand,
                units_per_box,
                max_order_qty,
            )
            counted = f" Unexpired stock is {current} (min {minimum}, max {maximum})."
            demand_text = (
                f" Replenishment targets about {math.ceil(target_stock)} pieces for the next {target} days, "
                f"using the {source.replace('_', ' ')} estimate; PO planner suggestion is {suggested_po_qty}."
            )
            reasoning = (
                f"{signal}.{counted}{demand_text} EOQ reorder suggestion is {qty}."
                if has_demand
                else f"{counted} No recent demand; the PO planner suggests {suggested_po_qty} pieces to reach at least the minimum stock."
            )
            if qty == 0:
                reasoning += (
                    f" No full box fits under the {MAX_ORDER_UNITS}-piece order cap "
                    "and maximum-stock level; review the package size or maximum stock."
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
        suggestions.append({
            "drug_id": int(row["drug_id"]),
            "generic_name": row["generic_name"],
            "brand_name": row["brand_name"],
            "dosage": row["dosage"],
            "form": row["form"],
            "category": row["category"],
            "procurement_type": procurement,
            "auto_po": auto_po,
            "current_stock": current,
            "minimum_stock": minimum,
            "maximum_stock": maximum,
            "units_per_box": units_per_box,
            "max_order_qty": max_order_qty,
            "suggested_box_qty": qty // units_per_box,
            "eoq_qty": eoq,
            "avg_daily_sales": round(avg, 2),
            "forecast_units": int(round(predicted)) if predicted is not None else None,
            "forecast_period_days": target,
            "demand_source": source,
            "days_of_stock": days_of_stock,
            "action": action,
            "suggested_qty": qty,
            "suggested_po_qty": suggested_po_qty,
            "suggested_reduce_qty": reduce_qty,
            "reasoning": reasoning,
            "suggested_supplier_id": int(row["suggested_supplier_id"]) if row["suggested_supplier_id"] is not None else None,
            "suggested_supplier_name": row["suggested_supplier_name"],
            "suggested_supplier_email": (row.get("suggested_supplier_email") or "").strip(),
            "open_po_numbers": row.get("open_po_numbers") or "",
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
            "order_qty_model": "eoq",
            "po_planner_qty_model": "forecast_demand_coverage",
            "max_order_units": MAX_ORDER_UNITS,
            "max_eoq_order_units": MAX_ORDER_UNITS,
            "eoq_order_cost": EOQ_ORDER_COST,
            "eoq_holding_rate": EOQ_HOLDING_RATE,
            "unexpired_stock_only": True,
        },
        "suggestions": suggestions,
    }


def create_purchase_orders_from_needs(
    needs,
    created_by: str,
    notes: str,
    raise_alerts: bool = False,
    order_date: date | None = None,
    expected_date: date | None = None,
    send_on_schedule: bool = False,
) -> dict:
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
            drug_ids = list({int(item["drug_id"]) for item in needs if item.get("drug_id")})
            cur.execute(
                """
                SELECT d.drug_id, d.minimum_stock,
                       COALESCE(d.maximum_stock, 0) AS maximum_stock,
                       COALESCE(d.units_per_box, 1) AS units_per_box,
                       COALESCE(stock.on_hand, 0) AS current_stock
                FROM drugs_master d
                LEFT JOIN (
                    SELECT drug_id, SUM(current_stock) AS on_hand
                    FROM inventory_lots
                    WHERE is_active = 1 AND expiration_date >= CURRENT_DATE
                    GROUP BY drug_id
                ) stock ON stock.drug_id = d.drug_id
                WHERE d.drug_id = ANY(%s) AND d.is_active = 1
                """,
                (drug_ids,),
            )
            stock_by_drug = {int(row["drug_id"]): row for row in cur.fetchall()}
            by_supplier = {}
            for item in needs:
                label = item.get("generic_name") or f"Drug #{item.get('drug_id')}"
                if item["drug_id"] in open_drugs:
                    skipped.append({"name": label, "reason": "already on an open PO"})
                    continue
                stock = stock_by_drug.get(int(item["drug_id"]))
                if not stock:
                    skipped.append({"name": label, "reason": "medicine is inactive or no longer exists"})
                    continue
                sid = item.get("suggested_supplier_id")
                if not sid:
                    skipped.append({"name": label, "reason": "no suggested supplier"})
                    continue
                minimum = int(stock["minimum_stock"] or 0)
                maximum = int(stock["maximum_stock"] or 0)
                if maximum <= minimum:
                    maximum = _default_maximum(minimum)
                units_per_box = max(1, int(stock["units_per_box"] or 1))
                max_boxes = _max_order_boxes(
                    maximum,
                    int(stock["current_stock"] or 0),
                    units_per_box,
                )
                requested_pieces = int(item.get("suggested_qty") or 0)
                if (
                    requested_pieces < units_per_box
                    or requested_pieces % units_per_box != 0
                    or requested_pieces // units_per_box > max_boxes
                ):
                    skipped.append({
                        "name": label,
                        "reason": (
                            "quantity exceeds the 200-piece or maximum-stock limit, "
                            "or is not a whole number of boxes; refresh and review the quantity"
                        ),
                    })
                    continue
                by_supplier.setdefault(int(sid), []).append({
                    **item,
                    "quantity_ordered_boxes": requested_pieces // units_per_box,
                })
            po_order_date = order_date or datetime.now(MANILA).date()
            expected = expected_date.isoformat() if expected_date else None
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
                        (po_id, supplier_id, status, order_date, expected_date, notes, created_by, send_on_schedule)
                    VALUES (%s, %s, 'Pending', %s, %s, %s, %s, %s)
                    """,
                    (
                        po_id,
                        supplier_id,
                        po_order_date.isoformat(),
                        expected,
                        notes,
                        created_by,
                        bool(send_on_schedule),
                    ),
                )
                for item in items:
                    item_id = next_id(cur, "purchase_order_items", "po_item_id")
                    cur.execute(
                        """
                        INSERT INTO purchase_order_items (po_item_id, po_id, drug_id, quantity_ordered, unit_cost)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (item_id, po_id, item["drug_id"], item["quantity_ordered_boxes"], None),
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
                        f"{po_number} created for {supplier['supplier_name']} ({len(items)} item(s)).",
                    )
                created.append({
                    "po_id": po_id,
                    "po_number": po_number,
                    "supplier_id": int(supplier_id),
                    "supplier_name": supplier["supplier_name"],
                    "item_count": len(items),
                })
    return {"created": created, "skipped": skipped}


def _is_low_stock(current: int, minimum: int, status: str = "") -> bool:
    """True when on-hand is at or below the minimum, including out of stock."""
    st = str(status or "").lower()
    if st in {"low", "out"}:
        return True
    if int(current or 0) <= 0:
        return True
    return int(minimum or 0) > 0 and int(current or 0) <= int(minimum or 0)


def _drug_is_low_stock(drug_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT stock_status, COALESCE(minimum_stock, 0) AS minimum_stock,
                       COALESCE((
                           SELECT SUM(current_stock) FROM inventory_lots
                           WHERE is_active = 1
                             AND expiration_date >= CURRENT_DATE
                             AND drug_id = dm.drug_id
                       ), 0) AS on_hand
                FROM drugs_master dm
                WHERE dm.drug_id = %s AND dm.is_active = 1
                """,
                (int(drug_id),),
            )
            row = cur.fetchone()
    if not row:
        return False
    return _is_low_stock(int(row["on_hand"] or 0), int(row["minimum_stock"] or 0), row.get("stock_status"))


def _low_stock_drug_ids() -> set[int]:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT dm.drug_id, dm.stock_status, COALESCE(dm.minimum_stock, 0) AS minimum_stock,
                       COALESCE((
                           SELECT SUM(current_stock) FROM inventory_lots
                           WHERE is_active = 1
                             AND expiration_date >= CURRENT_DATE
                             AND drug_id = dm.drug_id
                       ), 0) AS on_hand
                FROM drugs_master dm
                WHERE dm.is_active = 1
                """
            )
            rows = cur.fetchall()
    return {
        int(row["drug_id"])
        for row in rows
        if _is_low_stock(int(row["on_hand"] or 0), int(row["minimum_stock"] or 0), row.get("stock_status"))
    }


def _po_email_html(po: dict, items: list[dict]) -> str:
    rows = []
    for it in items:
        name = it.get("generic_name") or "Medicine"
        if it.get("brand_name"):
            name += f" ({it['brand_name']})"
        detail = " ".join(x for x in [str(it.get("dosage") or "").strip(), str(it.get("form") or "").strip()] if x)
        boxes = int(it.get("quantity_ordered") or 0)
        per_box = max(1, int(it.get("units_per_box") or 1))
        cell = "padding:6px 10px;border:1px solid #ddd;"
        rows.append(
            f"<tr><td style='{cell}'>{_esc(name)}</td><td style='{cell}'>{_esc(detail or '-')}</td>"
            f"<td style='{cell}text-align:right;'>{boxes:,}</td>"
            f"<td style='{cell}text-align:right;'>{boxes * per_box:,}</td></tr>"
        )
    th = "padding:6px 10px;border:1px solid #ddd;background:#f4f4f4;text-align:left;"
    notes = f"<p><strong>Notes:</strong> {_esc(str(po['notes']))}</p>" if po.get("notes") else ""
    contact = f"<p>Questions about this order? Contact us: {_esc(PHARMACY_CONTACT)}</p>" if PHARMACY_CONTACT else ""
    return (
        f"<p>Good day, {_esc(po['supplier_name'])}.</p>"
        f"<p>{_esc(PHARMACY_NAME)} would like to place the following purchase order.</p>"
        f"<p><strong>PO number:</strong> {_esc(po['po_number'])}<br>"
        f"<strong>Order date:</strong> {_esc(str(po['order_date']))}<br>"
        f"<strong>Expected delivery date:</strong> {_esc(str(po['expected_date'] or 'To be confirmed'))}</p>"
        f"<table style='border-collapse:collapse;font-size:14px;'>"
        f"<thead><tr><th style='{th}'>Medicine</th><th style='{th}'>Strength / form</th>"
        f"<th style='{th}text-align:right;'>Boxes</th><th style='{th}text-align:right;'>Total pieces</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
        f"{notes}"
        "<p>Please reply to confirm this order and your delivery date, or let us know if any item is unavailable.</p>"
        f"{contact}"
        f"<p>Thank you,<br>{_esc(PHARMACY_NAME)}</p>"
    )


def _record_po_send(po_id: int, po_number: str, supplier_name: str, ok: bool, message: str, actor: str) -> None:
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                if ok:
                    cur.execute(
                        "UPDATE purchase_orders SET sent_to_supplier_at = NOW(), sent_to_supplier_by = %s, send_error = NULL, send_started_at = NULL WHERE po_id = %s",
                        (actor, po_id),
                    )
                    write_activity_log(cur, "Email Purchase Order", f"Emailed {po_number} to {supplier_name}.", actor=actor)
                else:
                    cur.execute(
                        "UPDATE purchase_orders SET send_error = %s, send_started_at = NULL WHERE po_id = %s",
                        (message[:500], po_id),
                    )
                    write_activity_log(
                        cur, "Email Purchase Order Failed",
                        f"Could not email {po_number} to {supplier_name}: {message}", actor=actor,
                    )
                    if actor == "System":
                        _upsert_alert(
                            cur, "auto_po", f"po_mail:{po_id}", "info",
                            "Purchase order not emailed",
                            f"{po_number} for {supplier_name} was created but not emailed: {message} Retry from the Purchase Orders module once fixed.",
                        )
    except Exception:
        logger.exception("could not record PO email result for %s", po_number)


def send_po_to_supplier(po_id: int, actor: str = "System", resend: bool = False) -> dict:
    """Email a purchase order to the supplier's address on file and record that it was sent."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT po.po_id, po.status, po.order_date, po.expected_date, po.notes,
                       po.sent_to_supplier_at, po.send_started_at,
                       s.supplier_name, s.email
                FROM purchase_orders po
                JOIN suppliers s ON s.supplier_id = po.supplier_id
                WHERE po.po_id = %s
                """,
                (int(po_id),),
            )
            po = cur.fetchone()
            if not po:
                return {"success": False, "message": "Purchase order not found."}
            if po["status"] not in ("Pending", "Partially Received"):
                return {"success": False, "message": f"A {po['status']} purchase order cannot be emailed."}
            if po["sent_to_supplier_at"] and not resend:
                return {
                    "success": False,
                    "already_sent": True,
                    "message": "This purchase order was already emailed to the supplier.",
                }
            cur.execute(
                """
                UPDATE purchase_orders
                SET send_started_at = NOW(), send_error = NULL
                WHERE po_id = %s
                  AND (sent_to_supplier_at IS NULL OR %s = TRUE)
                  AND (
                      send_started_at IS NULL
                      OR send_started_at < NOW() - INTERVAL '15 minutes'
                  )
                RETURNING po_id
                """,
                (int(po_id), bool(resend)),
            )
            if not cur.fetchone():
                return {
                    "success": False,
                    "send_in_progress": True,
                    "message": "A supplier email for this PO is already being processed. Refresh the PO status shortly.",
                }
            cur.execute(
                """
                SELECT poi.quantity_ordered, COALESCE(d.units_per_box, 1) AS units_per_box,
                       d.generic_name, d.brand_name, d.dosage, d.form
                FROM purchase_order_items poi
                JOIN drugs_master d ON d.drug_id = poi.drug_id
                WHERE poi.po_id = %s
                ORDER BY poi.po_item_id
                """,
                (int(po_id),),
            )
            items = cur.fetchall()
    po = dict(po)
    po["po_number"] = f"PO-{int(po_id):05d}"
    supplier_name = po["supplier_name"]
    to_email = str(po.get("email") or "").strip()
    if "@" not in to_email:
        message = f"{supplier_name} has no email address on file. Add one under Suppliers, then send again."
        _record_po_send(int(po_id), po["po_number"], supplier_name, False, message, actor)
        return {"success": False, "no_email": True, "po_number": po["po_number"], "supplier_name": supplier_name, "message": message}
    if not items:
        _record_po_send(
            int(po_id),
            po["po_number"],
            supplier_name,
            False,
            "This purchase order has no items.",
            actor,
        )
        return {"success": False, "message": "This purchase order has no items."}
    result = send_mail(
        to_email,
        f"Purchase Order {po['po_number']} from {PHARMACY_NAME}",
        _po_email_html(po, [dict(i) for i in items]),
    )
    ok = bool(result.get("success"))
    message = "Sent." if ok else str(result.get("message") or "Email could not be sent.")
    _record_po_send(int(po_id), po["po_number"], supplier_name, ok, message, actor)
    return {
        "success": ok,
        "po_number": po["po_number"],
        "supplier_name": supplier_name,
        "to": to_email,
        "message": f"{po['po_number']} emailed to {supplier_name} ({to_email})." if ok else message,
    }


def create_auto_purchase_orders(
    drug_ids: set[int] | None = None,
    use_forecast: bool = True,
    require_prophet: bool = True,
    email_suppliers: bool = False,
    notes: str | None = None,
) -> dict:
    data = compute_reorder_suggestions(use_forecast=use_forecast)
    needs = [
        s
        for s in data["suggestions"]
        if s["action"] == "increase"
        and s["suggested_po_qty"] > 0
        and s.get("auto_po", True)
        and (not require_prophet or str(s.get("demand_source") or "") == "prophet")
    ]
    needs = [{**item, "suggested_qty": item["suggested_po_qty"]} for item in needs]
    if drug_ids is not None:
        needs = [s for s in needs if int(s["drug_id"]) in drug_ids]
    notes = notes or (
        "This is an automatic order based on available demand forecasts (Prophet when available, otherwise recent sales), capped at maximum stock."
    )
    result = create_purchase_orders_from_needs(needs, "System", notes, raise_alerts=True)
    emailed = not_sent = 0
    if email_suppliers:
        for po in result["created"]:
            outcome = send_po_to_supplier(int(po["po_id"]), "System")
            if outcome.get("success"):
                emailed += 1
            else:
                not_sent += 1
    return {
        "created": result["created"],
        "skipped": len(result["skipped"]),
        "needed": len(needs),
        "emailed": emailed,
        "not_sent": not_sent,
    }


def run_weekly_po_job() -> dict:
    """Email POs explicitly queued for the configured schedule."""
    with _restock_lock:
        try:
            email_result = send_scheduled_purchase_order_emails()
            result = {
                "created": [],
                "skipped": 0,
                "needed": 0,
                "emailed": email_result["sent"],
                "not_sent": email_result["failed"],
                "email_results": email_result["results"],
            }
            with get_conn() as conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    scan_stock_alerts(cur)
        except Exception:
            logger.exception("weekly PO job failed")
            result = {
                "created": [],
                "skipped": 0,
                "needed": 0,
                "emailed": 0,
                "not_sent": 0,
                "error": "failed",
            }
        stamp = {"at": datetime.now(MANILA).isoformat(), **result}
        _last_run["weekly_po"] = stamp
        _last_run["daily"] = stamp
        logger.info("weekly PO job: %s", json.dumps(stamp, default=str))
        return result


def run_po_schedule_if_due() -> dict:
    """Send queued POs once when the configured recurring schedule is due."""
    with get_conn() as lock_conn:
        locked = False
        try:
            with lock_conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT pg_try_advisory_lock(728119021) AS locked")
                locked = bool(cur.fetchone()["locked"])
                if not locked:
                    return {"success": True, "skipped": "another scheduler is processing POs"}
                lock_conn.commit()

                schedule, last_run = _read_po_schedule(cur)
                now = datetime.now(MANILA)
                due_date = _latest_due_schedule_date(schedule, now)
                if due_date.isoformat() <= last_run:
                    return {"success": True, "skipped": "no new scheduled PO run is due"}
                occurrence = due_date.isoformat()

            result = run_weekly_po_job()
            with lock_conn.cursor(cursor_factory=RealDictCursor) as cur:
                current_schedule, _ = _read_po_schedule(cur)
                if _schedule_signature(current_schedule) == _schedule_signature(schedule):
                    cur.execute(
                        "SELECT setting_value FROM app_settings WHERE setting_key = %s",
                        (PO_SCHEDULE_KEY,),
                    )
                    current_row = cur.fetchone()
                    current_value = json.loads(current_row["setting_value"])
                    current_value["last_run_occurrence"] = occurrence
                    cur.execute(
                        "UPDATE app_settings SET setting_value = %s, updated_at = NOW() WHERE setting_key = %s",
                        (json.dumps(current_value), PO_SCHEDULE_KEY),
                    )
            _last_run["po_schedule"] = {
                "at": datetime.now(MANILA).isoformat(),
                "occurrence": occurrence,
                **result,
            }
            return {"success": True, "occurrence": occurrence, "result": result}
        except Exception:
            logger.exception("scheduled PO run failed")
            return {"success": False, "message": "Scheduled PO run failed; see server logs."}
        finally:
            if locked and not lock_conn.closed:
                try:
                    with lock_conn.cursor() as cur:
                        cur.execute("SELECT pg_advisory_unlock(728119021)")
                    lock_conn.commit()
                except Exception:
                    logger.exception("could not release scheduled PO lock")


def send_scheduled_purchase_order_emails() -> dict:
    """Send unsent open POs explicitly queued for the configured schedule."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT po.po_id
                FROM purchase_orders po
                WHERE po.send_on_schedule IS TRUE
                  AND po.sent_to_supplier_at IS NULL
                  AND po.send_error IS NULL
                  AND po.status IN ('Pending', 'Partially Received')
                  AND po.order_date <= (NOW() AT TIME ZONE 'Asia/Manila')::date
                  AND (
                      po.send_started_at IS NULL
                      OR po.send_started_at < NOW() - INTERVAL '15 minutes'
                  )
                ORDER BY po.order_date, po.po_id
                """
            )
            due_po_ids = [int(row["po_id"]) for row in cur.fetchall()]

    sent = failed = 0
    results = []
    for po_id in due_po_ids:
        result = send_po_to_supplier(po_id, "System")
        if result.get("success"):
            sent += 1
        else:
            failed += 1
        results.append({
            "po_id": po_id,
            "success": bool(result.get("success")),
            "message": result.get("message"),
        })
    return {"due": len(due_po_ids), "sent": sent, "failed": failed, "results": results}


def run_restock_if_needed() -> dict:
    """Refresh stock alerts; PO creation and email wait for the configured schedule."""
    global _pending_all_drugs
    if not _restock_lock.acquire(blocking=False):
        schedule_auto_restock_check()
        return _last_run.get("restock") or {}
    try:
        with _restock_timer_lock:
            drug_ids = set(_pending_drug_ids)
            _pending_drug_ids.clear()
            all_drugs = _pending_all_drugs
            _pending_all_drugs = False
        # Stock changes can refresh alerts and forecasts, but PO creation and
        # supplier email only happen on the configured recurring schedule.
        result = {
            "created": [],
            "skipped": 0,
            "needed": 0,
            "emailed": 0,
            "not_sent": 0,
            "deferred": "scheduled",
            "checked_drugs": len(drug_ids) if not all_drugs else "all",
        }
        with get_conn() as conn:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                scan_stock_alerts(cur)
        stamp = {"at": datetime.now(MANILA).isoformat(), **result}
        _last_run["restock"] = stamp
        _last_run["daily"] = stamp
        logger.info("restock check: %s", result)
        return result
    except Exception:
        logger.exception("restock check failed")
        return _last_run.get("restock") or {}
    finally:
        _restock_lock.release()


async def _restock_in_request_threadpool() -> None:
    global _restock_task_scheduled
    await asyncio.sleep(1)
    from starlette.concurrency import run_in_threadpool

    try:
        await run_in_threadpool(run_restock_if_needed)
    finally:
        leftover = False
        with _restock_timer_lock:
            _restock_task_scheduled = False
            leftover = bool(_pending_drug_ids or _pending_all_drugs)
        if leftover:
            schedule_auto_restock_check()


def schedule_auto_restock_check(drug_id: int | None = None) -> None:
    """Queue a low-stock refresh without creating or emailing a PO immediately."""
    global _pending_all_drugs, _restock_task_scheduled
    if drug_id is not None and not _drug_is_low_stock(int(drug_id)):
        return
    with _restock_timer_lock:
        if drug_id is None:
            _pending_all_drugs = True
        else:
            _pending_drug_ids.add(int(drug_id))
        if _restock_task_scheduled:
            return
        _restock_task_scheduled = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        threading.Thread(target=run_restock_if_needed, name="pharmalink-restock", daemon=True).start()
        with _restock_timer_lock:
            _restock_task_scheduled = False
        return
    loop.create_task(_restock_in_request_threadpool())


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


def run_hourly_jobs(send_email: bool = True) -> dict:
    result = {"expired_lots": 0, "near_expiry_alerts": 0, "stock_alerts": 0}
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            sync_all_stock_status(cur)
            result["expired_lots"] = deactivate_expired_lots(cur)
            result["near_expiry_alerts"] = scan_near_expiry(cur)
            result["stock_alerts"] = scan_stock_alerts(cur)
    result["consignment"] = run_consignment_cross_check()
    if send_email:
        result["email"] = email_alert_digest(stock_and_expiry_only=True)
    _last_run["hourly"] = {"at": datetime.now(MANILA).isoformat(), **result}
    logger.info("hourly jobs: %s", result)
    for drug_id in _low_stock_drug_ids():
        schedule_auto_restock_check(int(drug_id))
    return result


def run_startup_jobs() -> dict:
    """Scan stock on boot without SMTP so Render can finish starting."""
    return run_hourly_jobs(send_email=False)


def run_daily_jobs() -> dict:
    hourly = run_hourly_jobs()
    mail = email_alert_digest()
    result = {
        "hourly": hourly,
        "purchase_orders": {
            "created": [],
            "skipped": 0,
            "needed": 0,
            "emailed": 0,
            "not_sent": 0,
            "deferred": "Purchase orders are created by an admin and emailed on the configured schedule.",
        },
        "email": mail,
    }
    _last_run["daily"] = {"at": datetime.now(MANILA).isoformat(), **result}
    logger.info("daily jobs: %s", json.dumps(result, default=str))
    return result


def _as_lot_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value or "").strip()[:10], "%Y-%m-%d").date()


def apply_received_lot(cur, drug_id: int, lot_number: str, expiration_date, qty: int, price, cost_price, po_id: int):
    """Add pieces only when the lot number and expiration date are both the same.

    A different expiration date, even by one day, is a new inventory row.
    An existing row keeps its selling price. Stock is added, never replaced.
    """
    from app.pricing import resolve_selling_price

    lot_number = str(lot_number or "").strip()
    exp = _as_lot_date(expiration_date)
    cur.execute(
        """
        SELECT lot_inventory_id, price, cost_price, current_stock, is_active, expiration_date
        FROM inventory_lots
        WHERE drug_id = %s AND lot_number = %s AND expiration_date = %s
        FOR UPDATE
        """,
        (drug_id, lot_number, exp),
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
        (lot_id, drug_id, lot_number, exp, qty, sell, cost_price, po_id),
    )
    return lot_id, "created"


NEAR_EXPIRY_DAYS = 90


def run_consignment_cross_check() -> dict:
    """Clear leftover return marks. The company does not use a consignment policy."""
    cleared = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE inventory_lots
                SET return_status = NULL
                WHERE return_status IS NOT NULL
                """
            )
            cleared = cur.rowcount or 0
    return {
        "success": True,
        "updated": 0,
        "returnable": 0,
        "non_returnable": 0,
        "cleared": cleared,
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
    po_schedule = get_po_schedule()
    return {
        "scheduler": "running" if _scheduler and _scheduler.running else "stopped",
        "timezone": "Asia/Manila",
        "auto_po_mode": "admin-created-scheduled-email",
        "auto_po_schedule": po_schedule,
        "auto_po_email_supplier": True,
        "last_run": _last_run,
        "alert_email": alert_email_status(),
    }


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    from apscheduler.schedulers.background import BackgroundScheduler
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
        refresh_prophet_reorder_forecast,
        IntervalTrigger(hours=12),
        id="pharmalink-prophet-reorder",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        run_po_schedule_if_due,
        IntervalTrigger(minutes=1),
        id="pharmalink-configured-po-schedule",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    get_po_schedule()
    scheduler.start()
    _scheduler = scheduler
    scheduler.add_job(run_startup_jobs, "date", run_date=datetime.now(MANILA) + timedelta(seconds=12), id="pharmalink-startup")
    scheduler.add_job(
        run_po_schedule_if_due,
        "date",
        run_date=datetime.now(MANILA) + timedelta(seconds=15),
        id="pharmalink-configured-po-schedule-startup",
        max_instances=1,
    )
    scheduler.add_job(
        refresh_prophet_reorder_forecast,
        "date",
        run_date=datetime.now(MANILA) + timedelta(seconds=20),
        id="pharmalink-prophet-reorder-startup",
        max_instances=1,
    )
    logger.info("automation scheduler started")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
