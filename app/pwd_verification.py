import hashlib
import hmac
import json
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


def create_pwd_verification_provider() -> PwdVerificationProvider | None:
    return None


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
