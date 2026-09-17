PAYMENT_CHANNELS = [
    ("cash", "Cash"),
    ("gcash", "GCash"),
    ("maya", "Maya"),
    ("card", "Card (Debit/Credit)"),
    ("bank", "Bank Transfer"),
]

PAYMENT_CHANNELS = [
    ("cash", "Cash"),
    ("gcash", "GCash"),
    ("maya", "Maya"),
    ("card", "Card (Debit/Credit)"),
    ("bank", "Bank Transfer"),
]

PAYMENT_METHOD_KEYS = {key for key, _ in PAYMENT_CHANNELS}
PAYMENT_METHOD_LABELS = {key: label for key, label in PAYMENT_CHANNELS}
CASHIER_PAYMENT_KEYS = {"cash", "gcash", "maya"}


def normalize_payment_method(value, default="cash"):
    method = str(value or default).strip().lower()
    if method not in PAYMENT_METHOD_KEYS:
        return None
    return method
