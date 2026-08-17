import smtplib
from email.message import EmailMessage

from app.config import Config


def send_email(to_address, subject, body):
    """
    Send a plain-text email via SMTP.

    Returns True if the message was handed to the SMTP server, False if email
    isn't configured (no SMTP_HOST) or sending failed. Callers should treat a
    False return as "couldn't email" and fall back gracefully (e.g. log the
    link in dev) rather than surfacing an error to the user.
    """
    if not Config.SMTP_HOST:
        return False

    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = Config.SMTP_FROM or Config.SMTP_USER
    message['To'] = to_address
    message.set_content(body)

    try:
        with smtplib.SMTP(Config.SMTP_HOST, Config.SMTP_PORT, timeout=10) as server:
            if Config.SMTP_USE_TLS:
                server.starttls()
            if Config.SMTP_USER:
                server.login(Config.SMTP_USER, Config.SMTP_PASSWORD)
            server.send_message(message)
        return True
    except Exception as e:
        print(f"[email] Failed to send to {to_address}: {e}")
        return False
