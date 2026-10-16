import os
import smtplib
import socket
from email.message import EmailMessage
from email.utils import formataddr


GENERIC_FORGOT_MESSAGE = (
    "If an account matches that ID and email, a password reset link has been sent to the email on file."
)

_SMTP_TIMEOUT = 10
_CONNECT_ERRORS = (
    TimeoutError,
    socket.timeout,
    OSError,
    smtplib.SMTPConnectError,
    smtplib.SMTPServerDisconnected,
)


def _clean(value: str) -> str:
    return (value or "").strip().strip('"').strip("'")


def _connect_ipv4_first(host, port, timeout, source_address=None):
    """Prefer IPv4. Render often has no IPv6 route, which raises errno 101."""
    last_err = None
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            infos = socket.getaddrinfo(host, port, family, socket.SOCK_STREAM)
        except OSError as exc:
            last_err = exc
            continue
        for af, socktype, proto, _canon, sa in infos:
            sock = socket.socket(af, socktype, proto)
            try:
                if timeout is not None:
                    sock.settimeout(timeout)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sa)
                return sock
            except OSError as exc:
                last_err = exc
                sock.close()
    if last_err:
        raise last_err
    raise OSError(101, "Network is unreachable")


class _SMTP(smtplib.SMTP):
    def _get_socket(self, host, port, timeout):
        return _connect_ipv4_first(host, port, timeout, self.source_address)


class _SMTP_SSL(smtplib.SMTP_SSL):
    def _get_socket(self, host, port, timeout):
        sock = _connect_ipv4_first(host, port, timeout, self.source_address)
        return self.context.wrap_socket(sock, server_hostname=host or self._host)


def _send_once(host: str, port: int, mode: str, user: str, password: str, msg: EmailMessage) -> None:
    if mode == "ssl":
        smtp = _SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT)
    else:
        smtp = _SMTP(host, port, timeout=_SMTP_TIMEOUT)
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
    with smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def send_mail(to_email: str, subject: str, body_html: str) -> dict:
    host = _clean(os.getenv("SMTP_HOST", "smtp.gmail.com")) or "smtp.gmail.com"
    port_raw = _clean(os.getenv("SMTP_PORT", "587")) or "587"
    try:
        port = int(port_raw)
    except (TypeError, ValueError):
        port = 587
    user = _clean(os.getenv("SMTP_EMAIL", ""))
    password = _clean(os.getenv("SMTP_APP_PASSWORD", "")).replace(" ", "")
    from_name = _clean(os.getenv("SMTP_FROM_NAME", "PharmaLink")) or "PharmaLink"
    to_email = _clean(to_email)

    if not user or not password:
        return {"success": False, "message": "SMTP is not configured."}
    if not to_email:
        return {"success": False, "message": "Missing recipient email."}

    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((from_name, user))
        msg["To"] = to_email
        msg.set_content("Please view this purchase order in an HTML email app.")
        msg.add_alternative(body_html or "", subtype="html")
    except Exception as exc:
        return {"success": False, "message": str(exc) or type(exc).__name__}

    mode = "ssl" if port == 465 else "starttls"
    try:
        _send_once(host, port, mode, user, password, msg)
        return {"success": True, "message": "sent"}
    except smtplib.SMTPAuthenticationError as exc:
        return {"success": False, "message": str(exc) or "SMTP login failed."}
    except _CONNECT_ERRORS as exc:
        if port != 465:
            try:
                _send_once(host, 465, "ssl", user, password, msg)
                return {"success": True, "message": "sent"}
            except Exception as fallback_exc:
                return {"success": False, "message": str(fallback_exc) or str(exc)}
        return {"success": False, "message": str(exc) or type(exc).__name__}
    except Exception as exc:
        return {"success": False, "message": str(exc) or type(exc).__name__}
