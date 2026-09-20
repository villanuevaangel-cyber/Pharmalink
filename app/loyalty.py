from psycopg2.extras import RealDictCursor

from app.db import fetch_one, get_conn

DEFAULT_PESO_PER_POINT = 0.30
SETTING_KEY = "loyalty_peso_per_point"
LEGACY_PERCENT_KEY = "loyalty_points_percent"


def ensure_loyalty_settings(conn) -> None:
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
        cur.execute("SELECT setting_value FROM app_settings WHERE setting_key = %s", (SETTING_KEY,))
        peso_row = cur.fetchone()
        if peso_row is None:
            cur.execute(
                "SELECT setting_value FROM app_settings WHERE setting_key = %s",
                (LEGACY_PERCENT_KEY,),
            )
            legacy = cur.fetchone()
            peso = DEFAULT_PESO_PER_POINT
            if legacy:
                try:
                    percent = float(legacy["setting_value"])
                    converted = round(percent / 100.0, 2)
                    if converted >= 0.01:
                        peso = converted
                except (TypeError, ValueError):
                    pass
            cur.execute(
                """
                INSERT INTO app_settings (setting_key, setting_value)
                VALUES (%s, %s)
                ON CONFLICT (setting_key) DO NOTHING
                """,
                (SETTING_KEY, f"{peso:.2f}"),
            )
    conn.commit()


def _settings_dict(peso: float) -> dict:
    peso = round(float(peso), 2)
    return {
        "peso_per_point": peso,
        "points_percent": round(peso * 100.0, 2),
    }


def parse_peso_per_point(raw) -> float:
    try:
        peso = float(raw)
    except (TypeError, ValueError):
        raise ValueError("Enter how many pesos one point is worth.") from None
    if peso < 0.01 or peso > 10:
        raise ValueError("Discount per point must be between ₱0.01 and ₱10.00.")
    return round(peso, 2)


def get_loyalty_settings() -> dict:
    row = fetch_one(
        "SELECT setting_value FROM app_settings WHERE setting_key = %s",
        (SETTING_KEY,),
    )
    try:
        peso = parse_peso_per_point(row["setting_value"] if row else DEFAULT_PESO_PER_POINT)
    except ValueError:
        peso = DEFAULT_PESO_PER_POINT
    return _settings_dict(peso)


def get_peso_per_point() -> float:
    return float(get_loyalty_settings()["peso_per_point"])


def save_loyalty_peso(peso: float) -> dict:
    value = parse_peso_per_point(peso)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO app_settings (setting_key, setting_value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (setting_key) DO UPDATE
                SET setting_value = EXCLUDED.setting_value, updated_at = NOW()
                """,
                (SETTING_KEY, f"{value:.2f}"),
            )
    return _settings_dict(value)
