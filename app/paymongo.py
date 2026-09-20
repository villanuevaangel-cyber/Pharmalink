import base64
import json
import os
import urllib.error
import urllib.request

API_BASE = "https://api.paymongo.com/v1"
METHOD_MAP = {"gcash": "gcash", "maya": "paymaya"}


class PayMongoError(Exception):
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def configured() -> bool:
    return bool(secret_key())


def mode() -> str:
    value = str(os.getenv("PAYMONGO_MODE") or "live").strip().lower()
    return "test" if value == "test" else "live"


def secret_key() -> str:
    if mode() == "test":
        return (
            os.getenv("PAYMONGO_TEST_SECRET_KEY")
            or os.getenv("PAYMONGO_SECRET_KEY")
            or ""
        ).strip()
    return (
        os.getenv("PAYMONGO_LIVE_SECRET_KEY")
        or os.getenv("PAYMONGO_SECRET_KEY")
        or ""
    ).strip()


def public_key() -> str:
    if mode() == "test":
        return (
            os.getenv("PAYMONGO_TEST_PUBLIC_KEY")
            or os.getenv("PAYMONGO_PUBLIC_KEY")
            or ""
        ).strip()
    return (
        os.getenv("PAYMONGO_LIVE_PUBLIC_KEY")
        or os.getenv("PAYMONGO_PUBLIC_KEY")
        or ""
    ).strip()


def paymongo_method(channel: str) -> str | None:
    return METHOD_MAP.get(str(channel or "").strip().lower())


def to_centavos(amount) -> int:
    return int(round(float(amount) * 100))


def from_centavos(amount) -> float:
    return round(int(amount or 0) / 100.0, 2)


def _request(method: str, path: str, payload: dict | None = None, api_key: str | None = None) -> dict:
    key = (api_key or secret_key()).strip()
    if not key:
        raise PayMongoError("PayMongo is not configured. Add the secret key to .env.", 503)
    token = base64.b64encode(f"{key}:".encode("utf-8")).decode("ascii")
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API_BASE + path,
        data=body,
        method=method,
        headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        detail = _error_message(raw) or f"PayMongo error ({exc.code})."
        raise PayMongoError(detail, 502) from exc
    except urllib.error.URLError as exc:
        raise PayMongoError("Could not reach PayMongo. Check the internet connection.", 502) from exc
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise PayMongoError("PayMongo returned an invalid response.", 502) from exc


def _error_message(raw: str) -> str:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return ""
    errors = data.get("errors") or []
    if not errors:
        return str(data.get("detail") or "")
    first = errors[0] or {}
    detail = first.get("detail") or first.get("title") or ""
    return str(detail)


def create_checkout_session(
    *,
    amount: float,
    channel: str,
    description: str,
    success_url: str,
    cancel_url: str,
    metadata: dict | None = None,
) -> dict:
    pm_type = paymongo_method(channel)
    if not pm_type:
        raise PayMongoError("Choose GCash or Maya.")
    centavos = to_centavos(amount)
    if centavos < 100:
        raise PayMongoError("E-wallet payments must be at least ₱1.00.")
    payload = {
        "data": {
            "attributes": {
                "send_email_receipt": False,
                "show_description": True,
                "show_line_items": True,
                "description": (description or "PharmaLink payment")[:255],
                "line_items": [
                    {
                        "currency": "PHP",
                        "amount": centavos,
                        "name": (description or "PharmaLink payment")[:100],
                        "quantity": 1,
                    }
                ],
                "payment_method_types": [pm_type],
                "success_url": success_url,
                "cancel_url": cancel_url,
                "metadata": metadata or {},
            }
        }
    }
    decoded = _request("POST", "/checkout_sessions", payload)
    data = (decoded.get("data") or {})
    attrs = data.get("attributes") or {}
    checkout_id = data.get("id")
    checkout_url = attrs.get("checkout_url")
    if not checkout_id or not checkout_url:
        raise PayMongoError("PayMongo did not return a checkout URL.")
    return {
        "id": checkout_id,
        "checkout_url": checkout_url,
        "status": attrs.get("status") or "unpaid",
        "amount": from_centavos(centavos),
        "channel": channel,
        "mode": mode(),
    }


def retrieve_checkout_session(checkout_id: str) -> dict:
    checkout_id = str(checkout_id or "").strip()
    if not checkout_id:
        raise PayMongoError("Missing PayMongo checkout id.")
    decoded = _request("GET", f"/checkout_sessions/{checkout_id}")
    data = (decoded.get("data") or {})
    attrs = data.get("attributes") or {}
    paid, reference, paid_amount = _paid_fields(attrs)
    return {
        "id": data.get("id") or checkout_id,
        "checkout_url": attrs.get("checkout_url"),
        "status": "paid" if paid else (attrs.get("status") or "unpaid"),
        "paid": paid,
        "reference": reference,
        "amount": paid_amount,
        "raw_status": attrs.get("status"),
    }


def _paid_fields(attrs: dict) -> tuple[bool, str, float | None]:
    payments = attrs.get("payments") or []
    for payment in payments:
        pattrs = (payment.get("attributes") or payment) if isinstance(payment, dict) else {}
        status = str(pattrs.get("status") or "").lower()
        if status in {"paid", "succeeded"}:
            pid = payment.get("id") or pattrs.get("id") or ""
            amount = pattrs.get("amount")
            return True, str(pid), from_centavos(amount) if amount is not None else None
    intent = attrs.get("payment_intent") or {}
    if isinstance(intent, dict):
        iattrs = intent.get("attributes") or intent
        status = str(iattrs.get("status") or "").lower()
        if status in {"succeeded", "paid"}:
            return True, str(intent.get("id") or ""), from_centavos(iattrs.get("amount")) if iattrs.get("amount") is not None else None
    status = str(attrs.get("status") or "").lower()
    if status in {"paid", "succeeded", "active_paid"}:
        return True, "", None
    return False, "", None


def require_paid_checkout(checkout_id: str, expected_amount: float, channel: str | None = None) -> dict:
    session = retrieve_payment(checkout_id)
    if not session.get("paid"):
        raise PayMongoError("E-wallet payment is not completed yet. Ask the customer to finish GCash or Maya.")
    paid_amount = session.get("amount")
    if paid_amount is not None and abs(paid_amount - float(expected_amount)) > 0.05:
        raise PayMongoError("Paid amount does not match this sale total.")
    session["channel"] = channel
    return session


def _next_action_qr_image(attrs: dict) -> str:
    next_action = attrs.get("next_action") or {}
    code = next_action.get("code") or {}
    image = ""
    if isinstance(code, dict):
        image = str(code.get("image_url") or "")
    if not image:
        image = str(next_action.get("image_url") or next_action.get("qr_code") or "")
    return image


def _intent_paid_fields(attrs: dict, intent_id: str) -> tuple[bool, str, float | None]:
    status = str(attrs.get("status") or "").lower()
    amount = attrs.get("amount")
    paid_amount = from_centavos(amount) if amount is not None else None
    payments = attrs.get("payments") or []
    for payment in payments:
        if not isinstance(payment, dict):
            continue
        pattrs = payment.get("attributes") or payment
        pstatus = str(pattrs.get("status") or "").lower()
        if pstatus in {"paid", "succeeded"}:
            return True, str(payment.get("id") or pattrs.get("id") or intent_id), paid_amount
    if status in {"succeeded", "paid"}:
        return True, intent_id, paid_amount
    return False, "", paid_amount


def create_qrph_payment(*, amount: float, description: str, metadata: dict | None = None) -> dict:
    centavos = to_centavos(amount)
    if centavos < 100:
        raise PayMongoError("E-wallet payments must be at least ₱1.00.")
    intent = _request(
        "POST",
        "/payment_intents",
        {
            "data": {
                "attributes": {
                    "amount": centavos,
                    "currency": "PHP",
                    "payment_method_allowed": ["qrph"],
                    "description": (description or "PharmaLink payment")[:255],
                    "metadata": metadata or {},
                }
            }
        },
    )
    intent_data = intent.get("data") or {}
    intent_id = intent_data.get("id")
    client_key = (intent_data.get("attributes") or {}).get("client_key")
    if not intent_id or not client_key:
        raise PayMongoError("PayMongo did not return a payment intent.")
    pk = public_key() or secret_key()
    method = _request(
        "POST",
        "/payment_methods",
        {"data": {"attributes": {"type": "qrph", "expiry_seconds": 1800}}},
        api_key=pk,
    )
    method_id = (method.get("data") or {}).get("id")
    if not method_id:
        raise PayMongoError("PayMongo could not create a QR Ph code. Enable QR Ph on your PayMongo account.")
    attached = _request(
        "POST",
        f"/payment_intents/{intent_id}/attach",
        {"data": {"attributes": {"payment_method": method_id, "client_key": client_key}}},
        api_key=pk,
    )
    attrs = (attached.get("data") or {}).get("attributes") or {}
    image = _next_action_qr_image(attrs)
    if not image:
        raise PayMongoError("PayMongo did not return a scannable QR Ph image.")
    paid, reference, paid_amount = _intent_paid_fields(attrs, intent_id)
    return {
        "id": intent_id,
        "kind": "payment_intent",
        "qr_image": image,
        "checkout_url": "",
        "status": "paid" if paid else (attrs.get("status") or "unpaid"),
        "paid": paid,
        "reference": reference,
        "amount": paid_amount if paid_amount is not None else from_centavos(centavos),
        "mode": mode(),
    }


def retrieve_payment_intent(intent_id: str) -> dict:
    intent_id = str(intent_id or "").strip()
    if not intent_id:
        raise PayMongoError("Missing PayMongo payment id.")
    decoded = _request("GET", f"/payment_intents/{intent_id}")
    data = decoded.get("data") or {}
    attrs = data.get("attributes") or {}
    paid, reference, paid_amount = _intent_paid_fields(attrs, data.get("id") or intent_id)
    return {
        "id": data.get("id") or intent_id,
        "kind": "payment_intent",
        "qr_image": _next_action_qr_image(attrs),
        "checkout_url": "",
        "status": "paid" if paid else (attrs.get("status") or "unpaid"),
        "paid": paid,
        "reference": reference,
        "amount": paid_amount,
        "raw_status": attrs.get("status"),
    }


def retrieve_payment(payment_id: str) -> dict:
    payment_id = str(payment_id or "").strip()
    if payment_id.startswith("pi_"):
        return retrieve_payment_intent(payment_id)
    return retrieve_checkout_session(payment_id)
