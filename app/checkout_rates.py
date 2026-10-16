from psycopg2.extras import RealDictCursor

from app.db import fetch_one, get_conn

DEFAULT_SC_PWD_PERCENT = 20.0
DEFAULT_VAT_PERCENT = 12.0
SC_PWD_KEY = "sc_pwd_discount_percent"
VAT_KEY = "vat_percent"


def ensure_checkout_rates(conn) -> None:
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                setting_key VARCHAR(80) PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        cur.executemany(
            """
            INSERT INTO app_settings (setting_key, setting_value)
            VALUES (%s, %s)
            ON CONFLICT (setting_key) DO NOTHING
            """,
            [
                (SC_PWD_KEY, f"{DEFAULT_SC_PWD_PERCENT:.2f}"),
                (VAT_KEY, f"{DEFAULT_VAT_PERCENT:.2f}"),
            ],
        )
    conn.commit()


def parse_percent(raw, label: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"Enter a {label} percent.") from None
    if value < 0 or value > 100:
        raise ValueError(f"{label} must be from 0 to 100.")
    return round(value, 2)


def _read_percent(key: str, default: float, label: str) -> float:
    row = fetch_one("SELECT setting_value FROM app_settings WHERE setting_key = %s", (key,))
    try:
        return parse_percent(row["setting_value"] if row else default, label)
    except ValueError:
        return default


def get_checkout_rates() -> dict:
    return {
        "sc_pwd_percent": _read_percent(SC_PWD_KEY, DEFAULT_SC_PWD_PERCENT, "Senior / PWD discount"),
        "vat_percent": _read_percent(VAT_KEY, DEFAULT_VAT_PERCENT, "VAT"),
    }


def save_checkout_rate(kind: str, raw) -> dict:
    if kind == "sc_pwd":
        key, label = SC_PWD_KEY, "Senior / PWD discount"
    elif kind == "vat":
        key, label = VAT_KEY, "VAT"
    else:
        raise ValueError("Unknown rate.")
    value = parse_percent(raw, label)
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app_settings (setting_key, setting_value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (setting_key) DO UPDATE
                SET setting_value = EXCLUDED.setting_value, updated_at = NOW()
                """,
                (key, f"{value:.2f}"),
            )
    return get_checkout_rates()
