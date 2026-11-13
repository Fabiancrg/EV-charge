"""E-mail (SMTP) settings and test message."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from .. import audit, mail
from ..auth import Principal, require
from ..database import get_db
from ..i18n import msg

router = APIRouter(prefix="/api/settings/email", tags=["settings"])


class MailSettings(BaseModel):
    enabled: bool = False
    host: str = Field(default="", max_length=255)
    port: int = Field(default=587, ge=1, le=65535)
    security: str = Field(default="starttls", pattern="^(starttls|ssl|none)$")
    username: str = Field(default="", max_length=255)
    password: str | None = Field(default=None, max_length=500)  # write-only; empty: unchanged
    clear_password: bool = False
    from_address: str = Field(default="", max_length=255)
    from_name: str = Field(default="EV Charge", max_length=100)
    verify_certificate: bool = True

    @field_validator("host", "username", "from_address", "from_name", mode="before")
    @classmethod
    def _none_is_empty(cls, value: object) -> object:
        return "" if value is None else value  # the form sends empty fields as null


class MailTest(MailSettings):
    to: str = Field(min_length=3, max_length=255)


def _out(db: Session) -> dict[str, Any]:
    return {**mail.get_config(db), "has_password": bool(mail.get_password(db))}


def _validate(body: MailSettings) -> None:
    if body.enabled and not (body.host.strip() and body.from_address.strip()):
        raise HTTPException(422, msg("To enable e-mail, fill in the SMTP server and the sender address"))
    if body.from_address and "@" not in body.from_address:
        raise HTTPException(422, msg("The sender address is not an e-mail address"))


@router.get("")
def get_mail(_: Principal = Depends(require("settings:manage")), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _out(db)


@router.put("")
def put_mail(body: MailSettings, request: Request, _: Principal = Depends(require("settings:manage")), db: Session = Depends(get_db)) -> dict[str, Any]:
    _validate(body)
    cfg = body.model_dump(exclude={"password", "clear_password"})
    cfg["host"], cfg["from_address"], cfg["username"] = cfg["host"].strip(), cfg["from_address"].strip(), cfg["username"].strip()
    mail.save_config(db, cfg, (body.password or "").strip() or None, body.clear_password)
    audit.record(db, request, "settings.email", details=cfg | {"password": "changed" if body.password else ("cleared" if body.clear_password else "unchanged")})
    db.commit()
    return _out(db)


@router.post("/test")
async def test_mail(body: MailTest, request: Request, me: Principal = Depends(require("settings:manage")), db: Session = Depends(get_db)) -> dict[str, str]:
    """Send a test message with the settings of the form (saved or not)."""
    _validate(body.model_copy(update={"enabled": True}))
    cfg = {**mail.get_config(db), **body.model_dump(exclude={"password", "clear_password", "to", "enabled"})}
    password = "" if body.clear_password else ((body.password or "").strip() or mail.get_password(db))
    to = [a.strip() for a in body.to.replace(";", ",").split(",") if a.strip()]
    text = (
        f"This is a test message from EV Charge, sent by {me.display_name} on {datetime.now():%Y-%m-%d %H:%M}.\n\n"
        f"Server: {cfg['host']}:{cfg['port']} ({cfg['security']})\nIf you read this, e-mail works."
    )
    try:
        await mail.send_mail(db, to, "EV Charge — test e-mail", text, cfg=cfg, password=password)
    except mail.MailError as e:
        audit.record(db, request, "settings.email_test", details={"to": to, "ok": False, "error": str(e)})
        db.commit()
        raise HTTPException(422, str(e)) from None
    audit.record(db, request, "settings.email_test", details={"to": to, "ok": True})
    db.commit()
    return {"message": f"Test e-mail sent to {', '.join(to)}"}
