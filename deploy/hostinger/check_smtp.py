"""Print safe SMTP configuration and network diagnostics on the Hostinger server."""
from __future__ import annotations

import errno
import socket
from pathlib import Path

from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]
settings = dotenv_values(ROOT / ".env")
host = str(settings.get("SMTP_HOST") or "smtp.gmail.com").strip()
try:
    port = int(settings.get("SMTP_PORT") or 587)
except (TypeError, ValueError):
    port = 587

credentials_ready = bool(
    str(settings.get("SMTP_EMAIL") or "").strip()
    and str(settings.get("SMTP_APP_PASSWORD") or "").strip()
)
resend_ready = bool(str(settings.get("RESEND_API_KEY") or "").strip())
print(
    f"SMTP configuration: server={host}, port={port}, "
    f"credentials={'configured' if credentials_ready else 'missing'}, "
    f"Resend fallback={'configured' if resend_ready else 'not configured'}"
)

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(5)
try:
    result = sock.connect_ex((host, port))
    if result == 0:
        print("SMTP network check: configured server is reachable.")
    else:
        print(f"SMTP network check: connection failed ({errno.errorcode.get(result, result)}).")
except OSError as exc:
    print(f"SMTP network check: {type(exc).__name__}.")
finally:
    sock.close()
