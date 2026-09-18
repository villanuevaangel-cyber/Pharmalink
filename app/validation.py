import re

NAME_RE = re.compile(r"^[A-Za-zÑñ][A-Za-zÑñ\s.'-]{0,48}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
PHONE_RE = re.compile(r"^(09\d{9}|\+639\d{9}|639\d{9}|9\d{9})$")
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{2,29}$")
CUSTOMER_TYPES = {"Regular", "Senior", "PWD", "Other"}
DRUG_FORMS = (
    "Tablet", "Capsule", "Syrup", "Suspension", "Drops", "Cream", "Ointment",
    "Gel", "Lotion", "Inhaler", "Injection", "Ampule", "Vial", "Sachet",
    "Suppository", "Patch", "Solution", "Powder", "Spray",
)
DOSAGE_UNITS = (
    "mg", "mcg", "g", "mL", "%", "IU", "mg/5mL", "mg/mL", "mcg/mL", "puff", "N/A",
)
DRUG_NAME_RE = re.compile(r"^[A-Za-z0-9Ññ][A-Za-z0-9Ññ\s.'/()+\-]{1,79}$")
BARCODE_RE = re.compile(r"^[A-Za-z0-9\-._]{4,64}$")
CATEGORY_RE = re.compile(r"^[A-Za-z0-9Ññ][A-Za-z0-9Ññ\s/&\-]{1,59}$")
PASSWORD_HINT = (
    "Password must be at least 8 characters and include an uppercase letter, "
    "a lowercase letter, a number, and a special character."
)


def validate_username(username: str) -> str | None:
    username = (username or "").strip()
    if not username:
        return "Username is required."
    if not USERNAME_RE.match(username):
        return "Username must start with a letter and be 3-30 characters (letters, numbers, dot, underscore, hyphen)."
    return None


def password_complexity_error(password: str) -> str | None:
    pw = password or ""
    if len(pw) < 8:
        return "Password must be at least 8 characters."
    if not re.search(r"[A-Z]", pw):
        return "Password must include at least one uppercase letter."
    if not re.search(r"[a-z]", pw):
        return "Password must include at least one lowercase letter."
    if not re.search(r"\d", pw):
        return "Password must include at least one number."
    if not re.search(r"[^A-Za-z0-9]", pw):
        return "Password must include at least one special character."
    return None


def _clean_phone(value: str) -> str:
    return re.sub(r"[\s\-()]", "", value or "")


def title_case_person_name(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip())
    if not value:
        return ""
    return " ".join(part[:1].upper() + part[1:].lower() if part else "" for part in value.split(" "))


def normalize_ph_mobile(value: str) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    if digits.startswith("63") and len(digits) >= 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if re.fullmatch(r"9\d{9}", digits):
        return "+63" + digits
    return None


def prepare_profile_fields(
    first_name: str,
    last_name: str,
    email: str,
    phone_number: str = "",
    address: str = "",
    middle_name: str = "",
    require_phone: bool = True,
    require_address: bool = True,
) -> tuple[str | None, dict]:
    packed = {
        "first_name": title_case_person_name(first_name),
        "last_name": title_case_person_name(last_name),
        "middle_name": title_case_person_name(middle_name),
        "email": (email or "").strip(),
        "phone_number": normalize_ph_mobile(phone_number) or _clean_phone(phone_number),
        "address": re.sub(r"\s+", " ", (address or "").strip()),
    }
    err = validate_profile_fields(
        packed["first_name"],
        packed["last_name"],
        packed["email"],
        packed["phone_number"],
        packed["address"],
        packed["middle_name"],
        require_phone=require_phone,
        require_address=require_address,
    )
    if not err and packed["phone_number"]:
        packed["phone_number"] = normalize_ph_mobile(packed["phone_number"]) or packed["phone_number"]
    return err, packed


def validate_profile_fields(
    first_name: str,
    last_name: str,
    email: str,
    phone_number: str = "",
    address: str = "",
    middle_name: str = "",
    require_phone: bool = True,
    require_address: bool = True,
) -> str | None:
    first_name = (first_name or "").strip()
    last_name = (last_name or "").strip()
    middle_name = (middle_name or "").strip()
    email = (email or "").strip()
    phone_number = _clean_phone(phone_number)
    address = (address or "").strip()

    if not first_name or not last_name:
        return "First name and last name are required."
    if not NAME_RE.match(first_name):
        return "First name can only contain letters, spaces, periods, apostrophes, or hyphens."
    if not NAME_RE.match(last_name):
        return "Last name can only contain letters, spaces, periods, apostrophes, or hyphens."
    if middle_name and not NAME_RE.match(middle_name):
        return "Middle name can only contain letters, spaces, periods, apostrophes, or hyphens."
    if not email:
        return "Email is required."
    if not EMAIL_RE.match(email):
        return "Please enter a valid email address."
    if require_phone or phone_number:
        if not phone_number:
            return "Phone number is required."
        if not PHONE_RE.match(phone_number):
            return "Enter a valid Philippine mobile number (+63 9XXXXXXXXX)."
    if require_address or address:
        if len(address) < 5:
            return "Address must be at least 5 characters."
        if len(address) > 200:
            return "Address must be 200 characters or less."
    return None


def validate_drug_fields(
    generic_name: str,
    dosage: str,
    form: str,
    category: str,
    minimum_stock,
    brand_name: str = "",
    barcode: str = "",
) -> str | None:
    generic_name = (generic_name or "").strip()
    brand_name = (brand_name or "").strip()
    dosage = (dosage or "").strip()
    form = (form or "").strip()
    category = (category or "").strip()
    barcode = (barcode or "").strip()

    if not generic_name:
        return "Generic name is required."
    if not DRUG_NAME_RE.match(generic_name):
        return "Generic name must be 2-80 characters (letters, numbers, spaces, and . ' / ( ) - +)."
    if brand_name and not DRUG_NAME_RE.match(brand_name):
        return "Brand name must be 2-80 characters (letters, numbers, spaces, and . ' / ( ) - +)."
    if not dosage:
        return "Dosage is required."
    if len(dosage) > 40:
        return "Dosage must be 40 characters or less."
    if not form:
        return "Form is required."
    if form not in DRUG_FORMS and (len(form) < 2 or len(form) > 40 or not DRUG_NAME_RE.match(form)):
        return "Choose a standard form, or enter a short custom form name."
    if not category:
        return "Category is required."
    if not CATEGORY_RE.match(category):
        return "Category must be 2-60 characters (letters, numbers, spaces, /, &, -)."
    try:
        min_stock = int(minimum_stock)
    except (TypeError, ValueError):
        return "Minimum stock must be a whole number."
    if min_stock < 0 or min_stock > 100000:
        return "Minimum stock must be between 0 and 100,000."
    if barcode and not BARCODE_RE.match(barcode):
        return "Barcode must be 4-64 characters (letters, numbers, dash, dot, underscore)."
    return None
