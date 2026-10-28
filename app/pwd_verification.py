import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Protocol


MAX_PWD_QR_PAYLOAD_LENGTH = 8192
PWD_VERIFICATION_PROOF_TTL_SECONDS = 1800


@dataclass(frozen=True)
class PwdVerificationResult:
    authenticated: bool
    id_number: str | None = None
    holder_name: str | None = None


class PwdVerificationProvider(Protocol):
    async def verify_qr(self, qr_payload: str) -> PwdVerificationResult:
        """Authenticate a scanned UPWDIS QR payload using an authorized provider."""


class PwdVerificationUnavailableError(Exception):
    pass


class PwdVerificationProviderError(Exception):
    pass


# PWD ID number format: RR-PPMM-BBB-NNNNNNN (region, province+municipality PSGC,
# barangay, 7-digit sequence), e.g. 13-7405-000-0000002. Dashes/spaces are optional.
PWD_ID_NUMBER_RE = re.compile(r"(?<!\d)(\d{2})[-\s]?(\d{4})[-\s]?(\d{3})[-\s]?(\d{7})(?!\d)")

DOH_PWD_REGISTRY_URL = "https://pwd.doh.gov.ph/tbl_pwd_id_verificationlist.php"


# --- ID classification -------------------------------------------------------------------
# PhilSys National ID (PCN) is 16 digits shown as 0000-0000-0000-0000, and its QR carries
# PhilSys/PSA fields. TIN IDs show a 9-12 digit TIN. Detect them (from QR text or OCR text) so
# the cashier gets a clear "this is not a PWD ID" message instead of silence.
PHILSYS_PCN_RE = re.compile(r"(?<!\d)\d{4}[-\s]\d{4}[-\s]\d{4}[-\s]\d{4}(?!\d)")
PHILSYS_HINTS = ("philsys", "pambansang pagkakakilanlan", "philippine identification", '"pcn"', "pcn:")
TIN_NUMBER_RE = re.compile(r"(?<!\d)\d{3}[-\s]\d{3}[-\s]\d{3}(?:[-\s]\d{3,5})?(?!\d)")
TIN_HINTS_RE = re.compile(r"tax identification|bureau of internal revenue|\btin\b|\bbir\b", re.I)
PWD_HINTS_RE = re.compile(r"persons? with disabilit|disability identification|\bpwd\b|\bncda\b|\bpdao\b", re.I)
OTHER_ID_HINTS_RE = re.compile(
    r"driver'?s?\s+licen[sc]e|land transportation|\bumid\b|social security|\bsss\b|passport|"
    r"professional regulation|postal id|voter'?s?|senior citizen|\bosca\b|philhealth|pag-?ibig|"
    r"\bnbi\b|police clearance",
    re.I,
)
_DASH_LIKE_RE = re.compile(r"[\u2010-\u2015\u2212_]")


def classify_id_text(text: str) -> str:
    """Classify scanned/typed/OCR text.

    Returns "pwd" (a PWD ID number is present), "philsys" (National ID), "tin" (TIN ID),
    "pwd_card" (looks like a PWD card but no readable number), "other" (another known ID),
    or "unknown".
    """
    raw = text or ""
    normalized = _DASH_LIKE_RE.sub("-", raw)
    lowered = normalized.lower()
    if (
        PHILSYS_PCN_RE.search(normalized)
        or any(hint in lowered for hint in PHILSYS_HINTS)
        or ('"issuer"' in lowered and "psa" in lowered)
    ):
        return "philsys"
    if PWD_ID_NUMBER_RE.search(normalized):
        return "pwd"
    if TIN_NUMBER_RE.search(normalized) or TIN_HINTS_RE.search(normalized):
        return "tin"
    if PWD_HINTS_RE.search(normalized):
        return "pwd_card"
    if OTHER_ID_HINTS_RE.search(normalized):
        return "other"
    return "unknown"


def detect_pwd_qr_type(qr_payload: str) -> str:
    return classify_id_text(qr_payload)


def extract_holder_name(qr_payload: str) -> str | None:
    """Best effort: if the QR is JSON with a name field, return it (max 120 chars)."""
    try:
        data = json.loads(qr_payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("name", "fullName", "full_name", "holder_name", "holderName"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:120]
    return None


def extract_pwd_id_number(qr_payload: str) -> str | None:
    """Find a PWD ID number inside whatever the QR/OCR text contains (number, URL, JSON, text).

    Returns it normalized as RR-PPMM-BBB-NNNNNNN, or None if no valid-looking number exists.
    """
    match = PWD_ID_NUMBER_RE.search(_DASH_LIKE_RE.sub("-", qr_payload or ""))
    if not match:
        return None
    return "-".join(match.groups())


class ManualPwdVerificationProvider:
    """Capstone provider: no DOH API is called.

    It (1) extracts and format-checks the PWD ID number from the scanned/typed payload and
    (2) relies on the pharmacist confirming the ID against the DOH PWD Registry (opened in a
    new tab) and on the physical card. The server only issues the discount proof after that
    confirmation. Replace with an API-backed provider if DOH/NCDA grants access.
    """

    requires_manual_confirmation = True

    async def verify_qr(self, qr_payload: str) -> PwdVerificationResult:
        if detect_pwd_qr_type(qr_payload) != "pwd":
            return PwdVerificationResult(authenticated=False)
        id_number = extract_pwd_id_number(qr_payload)
        if id_number is None:
            return PwdVerificationResult(authenticated=False)
        return PwdVerificationResult(
            authenticated=True,
            id_number=id_number,
            holder_name=extract_holder_name(qr_payload),
        )


def create_pwd_verification_provider() -> PwdVerificationProvider | None:
    mode = os.getenv("PWD_VERIFICATION_MODE", "manual").strip().lower()
    if mode in ("off", "disabled", "none"):
        return None
    return ManualPwdVerificationProvider()


def _verification_fingerprint(
    qr_payload: str,
    staff_id: int | str,
    id_number: str | None,
    holder_name: str | None,
    signing_key: str,
) -> str:
    claims = json.dumps(
        [qr_payload, str(staff_id), id_number, holder_name],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hmac.new(
        signing_key.encode("utf-8"),
        claims.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def create_pwd_verification_proof(
    qr_payload: str,
    staff_id: int | str,
    signing_key: str,
    *,
    id_number: str | None = None,
    holder_name: str | None = None,
    now: int | None = None,
) -> dict[str, int | str | None]:
    issued_at = int(time.time()) if now is None else now
    return {
        "claim_fingerprint": _verification_fingerprint(
            qr_payload, staff_id, id_number, holder_name, signing_key
        ),
        "staff_id": str(staff_id),
        "expires_at": issued_at + PWD_VERIFICATION_PROOF_TTL_SECONDS,
    }


def is_valid_pwd_verification_proof(
    proof: object,
    qr_payload: str,
    staff_id: int | str,
    signing_key: str,
    *,
    id_number: str | None = None,
    holder_name: str | None = None,
    now: int | None = None,
) -> bool:
    if not isinstance(proof, dict):
        return False
    fingerprint = proof.get("claim_fingerprint")
    proof_staff_id = proof.get("staff_id")
    expires_at = proof.get("expires_at")
    current_time = int(time.time()) if now is None else now
    if (
        not isinstance(fingerprint, str)
        or proof_staff_id != str(staff_id)
        or type(expires_at) is not int
        or expires_at <= current_time
    ):
        return False
    return hmac.compare_digest(
        fingerprint,
        _verification_fingerprint(qr_payload, staff_id, id_number, holder_name, signing_key),
    )


def validate_pwd_qr_payload(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Scan or enter the PWD ID QR code.")
    payload = value.strip()
    if not payload:
        raise ValueError("Scan or enter the PWD ID QR code.")
    if len(payload) > MAX_PWD_QR_PAYLOAD_LENGTH:
        raise ValueError("The PWD ID QR code is too large to verify.")
    return payload
