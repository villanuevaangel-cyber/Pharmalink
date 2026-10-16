"""Browser push notifications for staff alerts and customer order updates."""

import base64
import json
import logging
import os
import threading

from app.db import fetch_all, get_conn

logger = logging.getLogger("pharmalink.push")

_keys_lock = threading.Lock()


def ensure_push_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS push_vapid (
                id INTEGER PRIMARY KEY,
                public_key TEXT NOT NULL,
                private_key TEXT NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS push_subscriptions (
                subscription_id INTEGER PRIMARY KEY,
                user_id INTEGER,
                audience VARCHAR(20) NOT NULL DEFAULT 'staff',
                endpoint TEXT NOT NULL,
                p256dh TEXT,
                auth TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS push_subscriptions_endpoint_uidx
            ON push_subscriptions (endpoint)
            """
        )
    conn.commit()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _generate_keys() -> tuple[str, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    raw_private = key.private_numbers().private_value.to_bytes(32, "big")
    return _b64url(public), _b64url(raw_private)


def _private_key_loads(private_key: str) -> bool:
    try:
        from py_vapid import Vapid
        Vapid.from_string(private_key=private_key)
        return True
    except Exception:
        return False


def vapid_keys() -> tuple[str, str]:
    with _keys_lock:
        row = None
        with get_conn() as conn:
            ensure_push_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT public_key, private_key FROM push_vapid WHERE id = 1")
                row = cur.fetchone()
                if not row or not _private_key_loads(row[1]):
                    public_key, private_key = _generate_keys()
                    cur.execute(
                        """
                        INSERT INTO push_vapid (id, public_key, private_key)
                        VALUES (1, %s, %s)
                        ON CONFLICT (id) DO UPDATE
                        SET public_key = EXCLUDED.public_key,
                            private_key = EXCLUDED.private_key
                        """,
                        (public_key, private_key),
                    )
                    return public_key, private_key
        return row[0], row[1]


def public_key() -> str:
    return vapid_keys()[0]


def save_subscription(audience: str, endpoint: str, p256dh: str, auth_key: str, user_id=None) -> None:
    from app.db import next_id

    with get_conn() as conn:
        ensure_push_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE push_subscriptions
                SET p256dh = %s, auth = %s, user_id = %s, audience = %s
                WHERE endpoint = %s
                """,
                (p256dh, auth_key, user_id, audience, endpoint),
            )
            if cur.rowcount:
                return
            cur.execute(
                """
                INSERT INTO push_subscriptions (subscription_id, user_id, audience, endpoint, p256dh, auth)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (next_id(cur, "push_subscriptions", "subscription_id"), user_id, audience, endpoint, p256dh, auth_key),
            )


def delete_subscription(endpoint: str) -> None:
    if not endpoint:
        return
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM push_subscriptions WHERE endpoint = %s", (endpoint,))


def _claim_email() -> str:
    address = (os.getenv("SMTP_EMAIL") or "pharmalink.ph32@gmail.com").strip()
    if not address.startswith("mailto:"):
        address = "mailto:" + address
    return address


def _drop_endpoint(endpoint: str) -> None:
    try:
        delete_subscription(endpoint)
    except Exception:
        logger.exception("Could not drop push subscription")


def staff_link(alert_type: str, role_name: str = "") -> dict:
    """Where a staff push should open, matching the in-app bell."""
    kind = (alert_type or "").lower()
    is_admin = (role_name or "").strip().lower() == "admin"
    if kind in {"online_order", "order_paid"}:
        return {"url": "/cashier/cashier.html?notice=orders", "target": "orders", "filter": ""}
    if kind == "auto_po" and is_admin:
        return {"url": "/admin/admin.html#deliveries", "target": "deliveries", "filter": ""}
    inv_filter = ""
    if kind in {"expiring", "expiring_30"}:
        inv_filter = "expiring30"
    elif kind == "expiring_90":
        inv_filter = "expiring"
    elif kind in {"low", "low_stock", "out", "out_of_stock"}:
        inv_filter = "low"
    elif kind == "expired":
        inv_filter = "all"
    if is_admin:
        url = "/admin/admin.html#inventory"
        if inv_filter:
            url = f"/admin/admin.html?filter={inv_filter}#inventory"
        return {"url": url, "target": "inventory", "filter": inv_filter}
    return {"url": "/cashier/cashier.html", "target": "inventory", "filter": inv_filter}


def customer_order_url(order_id: int) -> str:
    return f"/customer/customer.html?notice=order&order={int(order_id)}"


def _deliver(items) -> None:
    if not items:
        return
    try:
        from pywebpush import WebPushException, webpush
    except Exception:
        logger.warning("pywebpush is not installed; push notifications are off")
        return
    try:
        _public, private_key = vapid_keys()
    except Exception:
        logger.exception("Push keys are not available")
        return
    claims = {"sub": _claim_email()}
    for row, payload in items:
        endpoint = row.get("endpoint")
        try:
            webpush(
                subscription_info={
                    "endpoint": endpoint,
                    "keys": {"p256dh": row.get("p256dh"), "auth": row.get("auth")},
                },
                data=json.dumps(payload),
                vapid_private_key=private_key,
                vapid_claims=claims,
                ttl=60 * 60 * 12,
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                _drop_endpoint(endpoint)
            else:
                logger.warning("Push failed: %s", exc)
        except Exception:
            logger.exception("Push failed")


def _send_async(items) -> None:
    if not items:
        return
    threading.Thread(target=_deliver, args=(items,), daemon=True).start()


def queue_staff_push(title: str, body: str, alert_type: str = "") -> None:
    try:
        rows = fetch_all(
            """
            SELECT ps.endpoint, ps.p256dh, ps.auth, COALESCE(r.role_name, '') AS role_name
            FROM push_subscriptions ps
            LEFT JOIN users u ON u.user_id = ps.user_id
            LEFT JOIN role r ON r.role_id = u.role_id
            WHERE ps.audience = 'staff'
            """
        )
    except Exception:
        logger.exception("Could not load staff push subscriptions")
        return
    items = []
    for row in rows:
        link = staff_link(alert_type, row.get("role_name") or "")
        items.append((
            row,
            {
                "title": title or "PharmaLink",
                "body": body or "",
                "url": link["url"],
                "target": link["target"],
                "filter": link["filter"],
                "tag": f"staff-{(alert_type or 'alert')}-{(body or '')[:40]}",
            },
        ))
    _send_async(items)


def queue_customer_push(customer_id: int, title: str, body: str, order_id: int = 0) -> None:
    if not customer_id:
        return
    try:
        rows = fetch_all(
            """
            SELECT endpoint, p256dh, auth
            FROM push_subscriptions
            WHERE audience = 'customer' AND user_id = %s
            """,
            (int(customer_id),),
        )
    except Exception:
        logger.exception("Could not load customer push subscriptions")
        return
    url = customer_order_url(order_id) if order_id else "/customer/customer.html?notice=orders"
    payload = {
        "title": title or "PharmaLink",
        "body": body or "",
        "url": url,
        "target": "orders",
        "orderId": int(order_id or 0),
        "tag": f"order-{int(order_id)}" if order_id else "customer",
    }
    _send_async([(row, payload) for row in rows])
