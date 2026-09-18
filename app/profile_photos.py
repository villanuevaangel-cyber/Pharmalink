import time
from pathlib import Path

import psycopg2
from fastapi.responses import Response

from app.db import fetch_one, get_conn

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AVATAR = "https://cdn-icons-png.flaticon.com/512/2922/2922510.png"
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp", "gif"}
MIME_BY_EXT = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}


def ensure_profile_photo_columns(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE staff_info ADD COLUMN IF NOT EXISTS profile_image_data BYTEA")
        cur.execute("ALTER TABLE staff_info ADD COLUMN IF NOT EXISTS profile_image_mime VARCHAR(80)")
        cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS profile_image_data BYTEA")
        cur.execute("ALTER TABLE customers ADD COLUMN IF NOT EXISTS profile_image_mime VARCHAR(80)")
    conn.commit()


def mime_for_ext(ext: str) -> str:
    return MIME_BY_EXT.get((ext or "").lower().lstrip("."), "image/jpeg")


def staff_photo_url(user_id: int) -> str:
    return f"/api/staff/profile-photo?u={int(user_id)}"


def customer_photo_url(customer_id: int) -> str:
    return f"/api/customer/profile-photo?u={int(customer_id)}"


def resolve_photo_url(stored: str | None, has_bytes: bool, api_url: str) -> str:
    if has_bytes:
        return api_url
    path = str(stored or "").strip()
    if path.startswith("/uploads/"):
        disk = ROOT / path.lstrip("/")
        if disk.is_file():
            return path
    if path.startswith("http://") or path.startswith("https://"):
        return path
    if path.startswith("/api/"):
        return path
    return DEFAULT_AVATAR


def _as_bytes(value):
    if value is None:
        return None
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, bytes):
        return value
    return bytes(value)


def store_staff_photo(user_id: int, content: bytes, ext: str) -> str:
    mime = mime_for_ext(ext)
    dest = ROOT / "uploads" / "profile_pictures"
    dest.mkdir(parents=True, exist_ok=True)
    filename = f"staff_{user_id}_{int(time.time())}.{ext}"
    (dest / filename).write_bytes(content)
    url = staff_photo_url(user_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE staff_info
                SET profile_image = %s, profile_image_data = %s, profile_image_mime = %s
                WHERE user_id = %s
                """,
                (url, psycopg2.Binary(content), mime, user_id),
            )
    return url


def store_customer_photo(customer_id: int, content: bytes, ext: str) -> str:
    mime = mime_for_ext(ext)
    dest = ROOT / "uploads" / "profile_pictures"
    dest.mkdir(parents=True, exist_ok=True)
    filename = f"customer_{customer_id}_{int(time.time())}.{ext}"
    (dest / filename).write_bytes(content)
    url = customer_photo_url(customer_id)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE customers
                SET profile_image = %s, profile_image_data = %s, profile_image_mime = %s
                WHERE customer_id = %s
                """,
                (url, psycopg2.Binary(content), mime, customer_id),
            )
    return url


def photo_response(data, mime: str | None, fallback_path: str | None = None) -> Response | None:
    raw = _as_bytes(data)
    if raw:
        return Response(
            content=raw,
            media_type=mime or "image/jpeg",
            headers={"Cache-Control": "private, max-age=0, must-revalidate"},
        )
    path = str(fallback_path or "").strip()
    if path.startswith("/uploads/"):
        disk = ROOT / path.lstrip("/")
        if disk.is_file():
            return Response(
                content=disk.read_bytes(),
                media_type=mime_for_ext(disk.suffix),
                headers={"Cache-Control": "private, max-age=0, must-revalidate"},
            )
    return None


def fetch_staff_photo_row(user_id: int):
    return fetch_one(
        """
        SELECT profile_image, profile_image_data, profile_image_mime,
               (profile_image_data IS NOT NULL) AS has_profile_photo
        FROM staff_info WHERE user_id = %s
        """,
        (user_id,),
    )


def fetch_customer_photo_row(customer_id: int):
    return fetch_one(
        """
        SELECT profile_image, profile_image_data, profile_image_mime,
               (profile_image_data IS NOT NULL) AS has_profile_photo
        FROM customers WHERE customer_id = %s
        """,
        (customer_id,),
    )
