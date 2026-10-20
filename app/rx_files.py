"""Shared prescription file paths and download responses."""
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parent.parent
RX_DIR = ROOT / "private_uploads" / "prescriptions"
LEGACY_RX_DIR = ROOT / "uploads" / "prescriptions"
RX_ID_DIR = ROOT / "private_uploads" / "prescription_ids"
RX_MEDIA = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


def rx_filename(raw) -> str:
    return Path(str(raw or "")).name


def rx_filepath(raw) -> Path:
    filename = rx_filename(raw)
    private_path = RX_DIR / filename
    return private_path if private_path.is_file() else LEGACY_RX_DIR / filename


def rx_id_filepath(raw) -> Path:
    return RX_ID_DIR / rx_filename(raw)


def rx_file_type(raw) -> str:
    ext = Path(rx_filename(raw)).suffix.lower().lstrip(".")
    return ext or ""


def rx_file_response(raw):
    filename = rx_filename(raw)
    if not filename:
        return JSONResponse({"success": False, "message": "Prescription not found."}, status_code=404)
    filepath = rx_filepath(filename)
    if not filepath.is_file():
        return JSONResponse({"success": False, "message": "File is no longer available."}, status_code=404)
    media = RX_MEDIA.get(filepath.suffix.lower(), "application/octet-stream")
    return FileResponse(
        filepath,
        media_type=media,
        filename=filename,
        content_disposition_type="inline",
    )


def rx_id_file_response(raw):
    filename = rx_filename(raw)
    if not filename:
        return JSONResponse({"success": False, "message": "ID document not found."}, status_code=404)
    filepath = rx_id_filepath(filename)
    if not filepath.is_file():
        return JSONResponse({"success": False, "message": "ID document is no longer available."}, status_code=404)
    media = RX_MEDIA.get(filepath.suffix.lower(), "application/octet-stream")
    return FileResponse(
        filepath,
        media_type=media,
        filename=filename,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-store"},
    )
