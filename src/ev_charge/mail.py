"""Outgoing e-mail through an SMTP server (Settings → E-mail).

Settings live in the settings table ("smtp"); the password is encrypted (secretbox).
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from typing import Any

from sqlalchemy.orm import Session

from .i18n import msg as _msg
from .pricing import get_setting, set_setting
from .secretbox import decrypt_json, encrypt_json

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "host": "",
    "port": 587,
    "security": "starttls",  # starttls | ssl | none
    "username": "",
    "from_address": "",
    "from_name": "EV Charge",
    "verify_certificate": True,
    "timeout": 15,
}


class MailError(Exception):
    """Shown to the user (bad settings, server refused…)."""


def get_config(db: Session) -> dict[str, Any]:
    return {**DEFAULTS, **get_setting(db, "smtp", {})}


def get_password(db: Session) -> str:
    return decrypt_json(get_setting(db, "smtp_secret", None)).get("password", "")


def save_config(db: Session, cfg: dict[str, Any], password: str | None, clear_password: bool = False) -> None:
    set_setting(db, "smtp", {k: cfg.get(k, v) for k, v in DEFAULTS.items()})
    if clear_password:
        set_setting(db, "smtp_secret", None)
    elif password:
        set_setting(db, "smtp_secret", encrypt_json({"password": password}))


def mail_enabled(db: Session) -> bool:
    cfg = get_config(db)
    return bool(cfg["enabled"] and cfg["host"] and cfg["from_address"])


def build_message(cfg: dict[str, Any], to: list[str], subject: str, text: str, html: str | None = None,
                  attachments: list[tuple[str, bytes, str]] | None = None) -> EmailMessage:  # fmt: skip
    msg = EmailMessage()
    msg["From"] = formataddr((cfg["from_name"] or "", cfg["from_address"]))
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=cfg["from_address"].split("@")[-1] or None)
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    for filename, data, mime in attachments or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype or "octet-stream", filename=filename)
    return msg


def _send(cfg: dict[str, Any], password: str, msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    if not cfg["verify_certificate"]:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    try:
        if cfg["security"] == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(cfg["host"], int(cfg["port"]), timeout=cfg["timeout"], context=context)
        else:
            server = smtplib.SMTP(cfg["host"], int(cfg["port"]), timeout=cfg["timeout"])
        with server:
            server.ehlo()
            if cfg["security"] == "starttls":
                server.starttls(context=context)
                server.ehlo()
            if cfg["username"]:
                server.login(cfg["username"], password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise MailError(_msg("The SMTP server refused the login ({code})", code=exc.smtp_code)) from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise MailError(_msg("The SMTP server refused the recipient(s): {recipients}", recipients=", ".join(exc.recipients))) from exc
    except smtplib.SMTPException as exc:
        raise MailError(_msg("SMTP error: {error}", error=exc)) from exc
    except ssl.SSLError as exc:
        raise MailError(_msg("TLS error ({reason}): check the security mode and port", reason=exc.reason or exc)) from exc
    except OSError as exc:
        raise MailError(_msg("Cannot reach {host}:{port} ({error})", host=cfg["host"], port=cfg["port"], error=exc)) from exc


async def send_mail(db: Session, to: list[str], subject: str, text: str, html: str | None = None,
                    attachments: list[tuple[str, bytes, str]] | None = None, cfg: dict[str, Any] | None = None,
                    password: str | None = None) -> None:  # fmt: skip
    """Send an e-mail with the saved settings (or the given ones, to test them before saving)."""
    cfg = cfg or get_config(db)
    if not cfg["host"] or not cfg["from_address"]:
        raise MailError(_msg("Set the SMTP server and the sender address first (Settings → E-mail)"))
    if not to:
        raise MailError(_msg("No recipient"))
    msg = build_message(cfg, to, subject, text, html, attachments)
    await asyncio.to_thread(_send, cfg, password if password is not None else get_password(db), msg)
