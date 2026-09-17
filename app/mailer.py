import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


GENERIC_FORGOT_MESSAGE = (
    "If an account matches that ID and email, a password reset link has been sent to the email on file."
)


def send_mail(to_email: str, subject: str, body_html: str) -> dict:
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    user = os.getenv("SMTP_EMAIL", "")
    password = os.getenv("SMTP_APP_PASSWORD", "").replace(" ", "")
    from_name = os.getenv("SMTP_FROM_NAME", "PharmaLink")

    if not user or not password:
        return {"success": False, "message": "SMTP is not configured."}

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{user}>"
    msg["To"] = to_email
    msg.attach(MIMEText(body_html, "html"))

    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(user, password)
            smtp.sendmail(user, [to_email], msg.as_string())
        return {"success": True, "message": "sent"}
    except Exception as exc:
        return {"success": False, "message": str(exc)}
