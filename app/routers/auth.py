from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.activity import log_event, write_activity_log
from app.db import fetch_one, get_conn, next_id
from app.mailer import GENERIC_FORGOT_MESSAGE, send_mail
from app.profile_photos import (
    customer_photo_url,
    resolve_photo_url,
    staff_photo_url,
)
from app.security import hash_password, verify_password
from app.validation import password_complexity_error, prepare_profile_fields

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    username: str
    password: str


class RegisterBody(BaseModel):
    first_name: str
    last_name: str
    username: str
    email: str
    phone_number: str
    password: str
    confirm_password: str
    middle_name: str = ""


class ForgotBody(BaseModel):
    email: str
    employee_id: str = ""


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


class ResetPasswordBody(BaseModel):
    token: str
    new_password: str
    confirm_password: str


def _session_user(request: Request) -> dict | None:
    user_id = request.session.get("user_id")
    if user_id is None or user_id == "":
        return None
    return {
        "user_id": user_id,
        "role": request.session.get("user_role"),
        "firstName": request.session.get("user_first_name"),
        "lastName": request.session.get("user_last_name"),
        "customer_id": request.session.get("customer_id"),
    }


@router.post("/login")
def login(body: LoginBody, request: Request):
    username = body.username.strip()
    password = body.password
    if not username or not password:
        return JSONResponse({"success": False, "message": "Username and password are required."})

    staff = fetch_one(
        """
        SELECT u.user_id, u.password, r.role_name, s.first_name, s.last_name
        FROM users u
        JOIN role r ON u.role_id = r.role_id
        LEFT JOIN staff_info s ON s.user_id = u.user_id
        WHERE u.is_active = 1 AND r.role_name != 'Customer'
          AND (u.username = %s OR LOWER(COALESCE(s.email, '')) = LOWER(%s))
        """,
        (username, username),
    )
    if staff and verify_password(password, staff["password"]):
        request.session.clear()
        request.session["user_id"] = staff["user_id"]
        request.session["user_role"] = staff["role_name"]
        request.session["user_first_name"] = staff["first_name"] or username
        request.session["user_last_name"] = staff["last_name"] or ""
        log_event("Login", f"Signed in as {staff['role_name']}.", request=request)
        return {
            "success": True,
            "role": staff["role_name"],
            "firstName": staff["first_name"] or username,
            "lastName": staff["last_name"] or "",
        }

    customer = fetch_one(
        """
        SELECT customer_id, first_name, last_name, password FROM customers
        WHERE is_active = 1
          AND (username = %s OR LOWER(username) = LOWER(%s) OR LOWER(COALESCE(email, '')) = LOWER(%s))
        """,
        (username, username, username),
    )
    if customer and verify_password(password, customer["password"]):
        import secrets
        request.session.clear()
        request.session["user_id"] = customer["customer_id"]
        request.session["customer_id"] = customer["customer_id"]
        request.session["user_role"] = "Customer"
        request.session["user_first_name"] = customer["first_name"]
        request.session["user_last_name"] = customer["last_name"] or ""
        request.session["customer_name"] = f"{customer['first_name']} {customer['last_name']}"
        request.session["order_token"] = secrets.token_hex(32)
        log_event("Login", "Signed in as Customer.", request=request)
        return {
            "success": True,
            "role": "Customer",
            "firstName": customer["first_name"],
            "lastName": customer["last_name"] or "",
        }

    return JSONResponse({"success": False, "message": "Invalid username or password."})


@router.get("/me")
def me(request: Request):
    user = _session_user(request)
    if not user:
        return JSONResponse({"success": False, "message": "Not logged in."}, status_code=401)
    extra = {}
    if str(user["role"]).lower() == "customer":
        row = fetch_one(
            """
            SELECT first_name, last_name, profile_image,
                   (profile_image_data IS NOT NULL) AS has_profile_photo
            FROM customers WHERE customer_id = %s
            """,
            (user["user_id"],),
        )
        if row:
            extra["profile_image"] = resolve_photo_url(
                row.get("profile_image"),
                bool(row.get("has_profile_photo")),
                customer_photo_url(user["user_id"]),
            )
            extra["firstName"] = row.get("first_name") or user["firstName"]
            extra["lastName"] = row.get("last_name") or user.get("lastName") or ""
            request.session["user_first_name"] = extra["firstName"]
            request.session["user_last_name"] = extra["lastName"]
        import secrets
        if not request.session.get("order_token"):
            request.session["order_token"] = secrets.token_hex(32)
        extra["order_token"] = request.session["order_token"]
    else:
        staff = fetch_one(
            """
            SELECT si.first_name, si.middle_name, si.last_name, si.email, si.phone_number, si.address,
                   si.profile_image, (si.profile_image_data IS NOT NULL) AS has_profile_photo, u.username
            FROM staff_info si
            JOIN users u ON si.user_id = u.user_id
            WHERE si.user_id = %s
            """,
            (user["user_id"],),
        )
        if staff:
            staff = dict(staff)
            photo = resolve_photo_url(
                staff.get("profile_image"),
                bool(staff.pop("has_profile_photo", False)),
                staff_photo_url(user["user_id"]),
            )
            staff["profile_image"] = photo
            extra["staff"] = staff
            extra["profile_image"] = photo
            extra["firstName"] = staff.get("first_name") or user["firstName"]
            extra["lastName"] = staff.get("last_name") or user.get("lastName") or ""
            request.session["user_first_name"] = extra["firstName"]
            request.session["user_last_name"] = extra["lastName"]
    return {"success": True, **user, **extra}


@router.post("/register")
def register(body: RegisterBody):
    first_name = body.first_name.strip()
    middle_name = (body.middle_name or "").strip()
    last_name = body.last_name.strip()
    username = body.username.strip()
    email = body.email.strip()
    phone = body.phone_number.strip()
    password = body.password
    confirm = body.confirm_password

    if not all([first_name, last_name, username, email, phone, password, confirm]):
        return {"success": False, "message": "All fields (except Middle Name) are required."}
    profile_err, packed = prepare_profile_fields(
        first_name, last_name, email, phone, "Registered via signup", middle_name, require_address=False
    )
    if profile_err:
        return {"success": False, "message": profile_err}
    first_name, last_name, middle_name = packed["first_name"], packed["last_name"], packed["middle_name"]
    email, phone = packed["email"], packed["phone_number"]
    if password != confirm:
        return {"success": False, "message": "Passwords do not match."}
    pw_err = password_complexity_error(password)
    if pw_err:
        return {"success": False, "message": pw_err}

    if fetch_one("SELECT user_id FROM users WHERE username = %s", (username,)):
        return {"success": False, "message": "Username is already taken. Please choose another one."}
    if fetch_one("SELECT customer_id FROM customers WHERE email = %s", (email,)):
        return {"success": False, "message": "Email address is already registered."}

    hashed = hash_password(password)
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                user_id = next_id(cur, "users", "user_id")
                cur.execute(
                    "INSERT INTO users (user_id, username, password, role_id) VALUES (%s, %s, %s, %s)",
                    (user_id, username, hashed, 3),
                )
                cur.execute(
                    """
                    INSERT INTO customers
                    (customer_id, first_name, middle_name, last_name, username, address, phone_number, customer_type, password, loyalty_points, email)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        user_id,
                        first_name,
                        middle_name,
                        last_name,
                        username,
                        "N/A",
                        phone,
                        "Regular",
                        hashed,
                        0.00,
                        email,
                    ),
                )
                write_activity_log(
                    cur,
                    "Register",
                    f"Created customer account '{username}'.",
                    actor=f"{first_name} {last_name}".strip(),
                )
        return {"success": True, "message": "Registration successful! You can now log in."}
    except Exception as exc:
        return {"success": False, "message": f"Registration failed: {exc}"}


def _token_hash(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _public_base(request: Request) -> str:
    import os

    return (os.getenv("PUBLIC_APP_URL") or str(request.base_url)).rstrip("/")


def _reset_email_html(first_name: str, reset_url: str, account_kind: str) -> str:
    first = str(first_name or "there").replace("<", "")
    kind = str(account_kind or "account").replace("<", "")
    return f"""
        <div style="font-family:Arial,sans-serif;max-width:480px;margin:0 auto;">
            <h2 style="color:#7c3aed;">Reset your PharmaLink password</h2>
            <p>Hi {first},</p>
            <p>We received a request to reset the password for your {kind} account.
            Click the button below to choose a new password. This link expires in 1 hour.</p>
            <p style="text-align:center;margin:28px 0;">
                <a href="{reset_url}"
                   style="background:#7c3aed;color:#fff;text-decoration:none;padding:12px 22px;border-radius:8px;font-weight:bold;display:inline-block;">
                   Reset password
                </a>
            </p>
            <p style="color:#6b7280;font-size:13px;">If the button does not work, copy and paste this link into your browser:<br>{reset_url}</p>
            <p style="color:#6b7280;font-size:13px;">If you did not request this, you can ignore this email. Your password will stay the same.</p>
        </div>"""


@router.post("/forgot-password")
def forgot_password(body: ForgotBody, request: Request):
    import secrets
    from datetime import datetime, timedelta, timezone

    employee_id = (body.employee_id or "").strip()
    email = (body.email or "").strip()
    if not employee_id or not email:
        return {"success": False, "message": "Please enter both your ID (or username) and email."}

    account_id = int(employee_id) if employee_id.isdigit() else -1
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=1)

    staff = fetch_one(
        """
        SELECT u.user_id, s.first_name, s.email, r.role_name
        FROM users u
        JOIN role r ON u.role_id = r.role_id
        LEFT JOIN staff_info s ON s.user_id = u.user_id
        WHERE u.is_active = 1 AND LOWER(r.role_name) != 'customer'
          AND LOWER(COALESCE(s.email, '')) = LOWER(%s)
          AND (
                s.staff_id = %s
             OR u.user_id = %s
             OR LOWER(u.username) = LOWER(%s)
          )
        LIMIT 1
        """,
        (email, account_id, account_id, employee_id),
    )
    customer = None if staff else fetch_one(
        """
        SELECT customer_id, first_name, email
        FROM customers
        WHERE customer_id = %s AND LOWER(COALESCE(email, '')) = LOWER(%s) AND is_active = 1
        LIMIT 1
        """,
        (account_id, email),
    )

    target_email = None
    first_name = "there"
    kind = "customer"
    if staff:
        target_email = staff["email"]
        first_name = staff["first_name"] or "there"
        role = str(staff.get("role_name") or "staff").strip()
        kind = "admin" if role.lower() == "admin" else "staff"
    elif customer:
        target_email = customer["email"]
        first_name = customer["first_name"]

    if target_email:
        with get_conn() as conn:
            with conn.cursor() as cur:
                if staff:
                    cur.execute(
                        "UPDATE password_reset_tokens SET used_at = NOW() WHERE user_id = %s AND used_at IS NULL",
                        (staff["user_id"],),
                    )
                    cur.execute(
                        """
                        INSERT INTO password_reset_tokens (token_hash, user_id, expires_at)
                        VALUES (%s, %s, %s)
                        """,
                        (_token_hash(token), staff["user_id"], expires),
                    )
                else:
                    cur.execute(
                        "UPDATE password_reset_tokens SET used_at = NOW() WHERE customer_id = %s AND used_at IS NULL",
                        (customer["customer_id"],),
                    )
                    cur.execute(
                        """
                        INSERT INTO password_reset_tokens (token_hash, customer_id, expires_at)
                        VALUES (%s, %s, %s)
                        """,
                        (_token_hash(token), customer["customer_id"], expires),
                    )
        reset_url = f"{_public_base(request)}/reset-password.html?token={token}"
        sent = send_mail(
            target_email,
            "Reset your PharmaLink password",
            _reset_email_html(first_name, reset_url, kind),
        )
        if not sent.get("success"):
            return {"success": False, "message": "We couldn't send the reset email right now. Please try again later."}
        log_event(
            "Password Reset Requested",
            f"{kind.title()} account requested a password reset.",
            actor=str(first_name or "User"),
        )

    return {"success": True, "message": GENERIC_FORGOT_MESSAGE}


@router.post("/reset-password")
def reset_password(body: ResetPasswordBody):
    from datetime import datetime, timezone

    token = (body.token or "").strip()
    new, confirm = body.new_password, body.confirm_password
    if not token:
        return {"success": False, "message": "This reset link is invalid or incomplete."}
    if not new or not confirm:
        return {"success": False, "message": "Please enter and confirm your new password."}
    if new != confirm:
        return {"success": False, "message": "New passwords do not match."}
    pw_err = password_complexity_error(new)
    if pw_err:
        return {"success": False, "message": pw_err}

    row = fetch_one(
        """
        SELECT customer_id, user_id, expires_at, used_at
        FROM password_reset_tokens
        WHERE token_hash = %s
        """,
        (_token_hash(token),),
    )
    if not row or row.get("used_at"):
        return {"success": False, "message": "This reset link is invalid or has already been used."}
    expires = row["expires_at"]
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires < datetime.now(timezone.utc):
        return {"success": False, "message": "This reset link has expired. Please request a new one."}

    hashed = hash_password(new)
    with get_conn() as conn:
        with conn.cursor() as cur:
            if row.get("user_id"):
                cur.execute(
                    "UPDATE users SET password = %s WHERE user_id = %s",
                    (hashed, row["user_id"]),
                )
            elif row.get("customer_id"):
                cur.execute(
                    "UPDATE customers SET password = %s WHERE customer_id = %s",
                    (hashed, row["customer_id"]),
                )
            else:
                return {"success": False, "message": "This reset link is invalid or incomplete."}
            cur.execute(
                "UPDATE password_reset_tokens SET used_at = NOW() WHERE token_hash = %s",
                (_token_hash(token),),
            )
            who = "staff" if row.get("user_id") else "customer"
            write_activity_log(
                cur,
                "Reset Password",
                f"Completed password reset for a {who} account.",
                actor="User",
            )
    return {"success": True, "message": "Password updated. You can now log in with your new password."}


@router.post("/change-password")
def change_password(body: ChangePasswordBody, request: Request):
    user = _session_user(request)
    if not user:
        return {"success": False, "message": "Not logged in."}
    current, new, confirm = body.current_password, body.new_password, body.confirm_password
    if not current or not new or not confirm:
        return {"success": False, "message": "All password fields are required."}
    if new != confirm:
        return {"success": False, "message": "New passwords do not match."}
    pw_err = password_complexity_error(new)
    if pw_err:
        return {"success": False, "message": pw_err}
    if new == current:
        return {"success": False, "message": "New password must be different from the current password."}

    is_customer = str(user["role"]).lower() == "customer"
    table = "customers" if is_customer else "users"
    id_col = "customer_id" if is_customer else "user_id"
    row = fetch_one(f"SELECT password FROM {table} WHERE {id_col} = %s", (user["user_id"],))
    if not row or not verify_password(current, row["password"]):
        return {"success": False, "message": "Current password is incorrect."}

    hashed = hash_password(new)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE {table} SET password = %s WHERE {id_col} = %s", (hashed, user["user_id"]))
            write_activity_log(cur, "Change Password", "Changed account password.", request=request)
    return {"success": True, "message": "Password updated successfully."}


@router.get("/logout")
@router.post("/logout")
def logout(request: Request):
    if request.session.get("user_id") or request.session.get("customer_id"):
        log_event("Logout", "Signed out.", request=request)
    request.session.clear()
    return RedirectResponse(url="/", status_code=302)
