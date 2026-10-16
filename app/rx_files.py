"""Shared prescription file paths and download responses."""
from pathlib import Path

from fastapi.responses import FileResponse, JSONResponse

ROOT = Path(__file__).resolve().parent.parent
RX_DIR = ROOT / "uploads" / "prescriptions"
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
    return RX_DIR / rx_filename(raw)


def rx_file_type(raw) -> str:
    ext = Path(rx_filename(raw)).suffix.lower().lstrip(".")
    return ext or ""


def rx_file_response(raw):
    filename = rx_filename(raw)
    if not filename:
        return JSONResponse({"success": False, "message": "Prescription not found."}, status_code=404)
    filepath = RX_DIR / filename
    if not filepath.is_file():
        return JSONResponse({"success": False, "message": "File is no longer available."}, status_code=404)
    media = RX_MEDIA.get(filepath.suffix.lower(), "application/octet-stream")
    return FileResponse(
        filepath,
        media_type=media,
        filename=filename,
        content_disposition_type="inline",
    )
