"""
modules/mailer.py — Email sending via LAN SMTP relay.

Relay at 192.168.1.24:25 accepts unauthenticated connections from the LAN;
no TLS or SASL needed inside the network.

SMTP settings are resolved at send time from (highest priority first):
  1. app_settings.json (editable via the Settings page)
  2. Environment variables
  3. Config module defaults
"""
import json
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import APP_SETTINGS_PATH, SENDER_NAME, SMTP_FROM, SMTP_HOST, SMTP_PORT


def _live_cfg() -> dict:
    """Return SMTP config, with app_settings.json overriding env/config defaults."""
    cfg = {
        "host": SMTP_HOST,
        "port": SMTP_PORT,
        "from_addr": SMTP_FROM,
        "sender_name": SENDER_NAME,
    }
    if os.path.exists(APP_SETTINGS_PATH):
        try:
            with open(APP_SETTINGS_PATH, encoding="utf-8") as f:
                saved = json.load(f)
            if "smtp_host" in saved:
                cfg["host"] = saved["smtp_host"]
            if "smtp_port" in saved:
                cfg["port"] = int(saved["smtp_port"])
            if "smtp_from" in saved:
                cfg["from_addr"] = saved["smtp_from"]
            if "sender_name" in saved:
                cfg["sender_name"] = saved["sender_name"]
        except Exception:
            pass
    return cfg


def send_email(to_addr: str, subject: str, body: str) -> None:
    """Send a plain-text email via the LAN SMTP relay.

    Raises smtplib.SMTPException (or socket errors) on failure — callers
    should catch and return a user-friendly error.
    """
    cfg = _live_cfg()
    msg = MIMEMultipart()
    msg["From"] = f"{cfg['sender_name']} <{cfg['from_addr']}>"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    with smtplib.SMTP(cfg["host"], cfg["port"], timeout=10) as server:
        server.sendmail(cfg["from_addr"], [to_addr], msg.as_string())
