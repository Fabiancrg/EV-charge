"""Getting new people in: sign-up requests and invitations, both by e-mail.

- **Sign-up request** (public, when enabled by an administrator): someone asks for a group of their own. They
  confirm their e-mail address (a link), then an administrator approves — the group and its first manager are
  created, and that person receives a link to set a password — or rejects.
- **Invitation**: a group manager (or an administrator) invites someone to their group by e-mail, within the
  group's member limit. The link lets the person choose a username and a password.

Links carry a random token; only its SHA-256 is stored. Both need the e-mail settings (Settings → E-mail).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import audit, i18n, mail
from ..auth import Principal, _recent, check_password_strength, client_ip, hash_password, principal_for, require, start_session
from ..database import get_db
from ..i18n import msg
from ..i18n import translate as tr
from ..models import Car, CarGrant, Group, Invitation, Role, SignupRequest, User
from ..pricing import get_setting, set_setting
from ..schemas import CarGrantIO, Me
from ..sso import local_login_allowed
from .auth import me_out
from .users import check_group_size, manager

router = APIRouter(tags=["sign-up and invitations"])

VERIFY_HOURS = 48  # to click the link of the verification e-mail
INVITATION_DAYS = 7
MAX_REQUESTS_PER_ADDRESS = 5  # sign-up requests per client address, per quarter of an hour
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
USERNAME = r"^[A-Za-z0-9._@-]+$"
SETTINGS = {"enabled": False, "default_max_members": 5}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _settings(db: Session) -> dict[str, Any]:
    return {**SETTINGS, **get_setting(db, "signup", {})}


def signup_enabled(db: Session) -> bool:
    return bool(_settings(db)["enabled"]) and mail.mail_enabled(db)


def _origin(request: Request) -> str:
    """The public address of this site, for the links sent by e-mail: the one the browser used for this request
    (behind a reverse proxy the server doesn't know it)."""
    origin = request.headers.get("origin") or ""
    if origin and urlsplit(origin).netloc == request.headers.get("host"):
        return origin.rstrip("/")
    return str(request.base_url).rstrip("/")


async def _send(db: Session, to: str, language: str | None, subject: str, lines: list[str], link: str | None = None, button: str = "") -> None:
    """An e-mail of a few lines and, usually, a button to a link; subject, lines and button already in the recipient's language."""
    text = "\n\n".join(lines + ([f"{button}: {link}"] if link else []) + ["— EV Charge"])
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # noqa: E731
    html = "".join(f"<p>{esc(line)}</p>" for line in lines)
    if link:
        html += (
            f'<p><a href="{esc(link)}" style="display:inline-block;padding:10px 18px;background:#4f6df5;color:#fff;border-radius:8px;text-decoration:none">{esc(button)}</a></p>'
        )
        html += f'<p style="color:#666;font-size:12px">{esc(tr(language, "If the button doesn't work, copy this address into your browser:"))}<br>{esc(link)}</p>'
    try:
        await mail.send_mail(db, [to], subject, text, f'<div style="font-family:sans-serif;font-size:14px">{html}<p style="color:#666">— EV Charge</p></div>')
    except mail.MailError as exc:
        raise HTTPException(502, tr(language, "The e-mail couldn't be sent ({error})", error=i18n.localize(language, str(exc)))) from None


def _hello(language: str | None, name: str | None) -> str:
    return tr(language, "Hello {name},", name=name) if name else tr(language, "Hello,")


def _email(value: str) -> str:
    value = value.strip().lower()
    if not EMAIL.match(value):
        raise ValueError("Enter a valid e-mail address")
    return value


def _pending_invitations(db: Session, group_id: int) -> list[Invitation]:
    now = datetime.now(UTC)
    rows = db.scalars(select(Invitation).where(Invitation.group_id == group_id, Invitation.accepted_at.is_(None)))
    return [i for i in rows if _aware(i.expires_at) > now]


def _check_room(db: Session, group: Group, extra: int = 1) -> None:
    """Members and people already invited must fit in the group's limit."""
    members = db.scalar(select(func.count()).select_from(User).where(User.group_id == group.id, User.active.is_(True))) or 0
    invited = sum(1 for i in _pending_invitations(db, group.id) if i.user_id is None)
    if members + invited + extra > group.max_members:
        raise HTTPException(
            409,
            msg(
                "The group '{name}' is limited to {max_members} member(s), invitations included: ask an administrator to raise the limit",
                name=group.name,
                max_members=group.max_members,
            ),
        )


# ── Sign-up requests: public part ────────────────────────────────────────────


class SignupIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=100)
    email: str = Field(max_length=200)
    group_name: str = Field(min_length=2, max_length=100)
    message: str | None = Field(default=None, max_length=1000)

    _trim = field_validator("full_name", "group_name", mode="before")(lambda v: v.strip() if isinstance(v, str) else v)
    _mail = field_validator("email", mode="after")(_email)


class TokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=200)


@router.get("/api/signup/state")
def signup_state(db: Session = Depends(get_db)) -> dict[str, bool]:
    return {"enabled": signup_enabled(db)}


@router.post("/api/signup", status_code=202)
async def request_signup(body: SignupIn, request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    """Ask for a group. Answers the same whatever the address, so nobody learns who has an account here."""
    lang = i18n.language_of(request)
    if not signup_enabled(db):
        raise HTTPException(403, tr(lang, "Sign-up requests are closed"))
    key = f"signup:{client_ip(request) or '?'}"
    if len(_recent(key)) >= MAX_REQUESTS_PER_ADDRESS:
        raise HTTPException(429, tr(lang, "Too many requests: try again in a few minutes"))
    _recent(key).append(time.monotonic())
    answer = {"detail": tr(lang, "Check your mailbox: a link to confirm your address was sent")}
    if db.scalar(select(User.id).where(func.lower(User.email) == body.email)):
        return answer  # already has an account: nothing to do (and nothing to tell)
    existing = db.scalar(select(SignupRequest).where(SignupRequest.email == body.email, SignupRequest.status.in_(("unverified", "pending"))))
    if existing is not None and existing.status == "pending":
        return answer  # already waiting for the administrator
    token = secrets.token_urlsafe(32)
    req = existing or SignupRequest(email=body.email)
    req.full_name, req.group_name, req.message = body.full_name, body.group_name, (body.message or "").strip() or None
    req.status, req.token_hash, req.ip, req.created_at, req.language = "unverified", _hash(token), client_ip(request), datetime.now(UTC), lang
    db.add(req)
    await _send(
        db, body.email, lang, tr(lang, "Confirm your e-mail address"),
        [_hello(lang, body.full_name),
         tr(lang, "You asked for access to EV Charge for “{group}”. Confirm your e-mail address to send your request to the administrator.", group=body.group_name),
         tr(lang, "The link is valid for {hours} hours. If you didn't ask for this, ignore this e-mail.", hours=VERIFY_HOURS)],
        f"{_origin(request)}/signup/verify?token={token}", tr(lang, "Confirm my address"),
    )  # fmt: skip
    db.commit()
    return answer


@router.post("/api/signup/verify")
async def verify_signup(body: TokenIn, request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    """The link of the verification e-mail: the request now waits for an administrator."""
    lang = i18n.language_of(request)
    req = db.scalar(select(SignupRequest).where(SignupRequest.token_hash == _hash(body.token)))
    if req is None:
        raise HTTPException(404, tr(lang, "This link isn't valid (already used, or replaced by a newer one)"))
    if req.status == "unverified":
        if datetime.now(UTC) - _aware(req.created_at) > timedelta(hours=VERIFY_HOURS):
            raise HTTPException(410, tr(lang, "This link has expired: fill in the request again"))
        req.status, req.verified_at = "pending", datetime.now(UTC)
        admins = [(u.email, u.language) for u in db.scalars(select(User).where(User.active.is_(True))) if u.email and principal_for(u).has("users:manage")]
        db.commit()
        for address, theirs in admins:  # best effort: the request is recorded whatever happens
            try:
                line = tr(theirs, "{name} <{email}> asks for a group “{group}”.", name=req.full_name, email=req.email, group=req.group_name)
                await mail.send_mail(db, [address], tr(theirs, "EV Charge: a sign-up request to review"), f"{line}\n\n{_origin(request)}/signup-requests\n")
            except mail.MailError:
                pass
    return {"status": req.status, "detail": tr(lang, "Your address is confirmed: an administrator will review your request and you will receive an e-mail")}


# ── Sign-up requests: administrators ─────────────────────────────────────────


class SignupSettings(BaseModel):
    enabled: bool = False
    default_max_members: int = Field(default=5, ge=1, le=1000)


class Approval(BaseModel):
    username: str = Field(min_length=2, max_length=50, pattern=USERNAME)
    group_name: str = Field(min_length=2, max_length=100)
    max_members: int = Field(default=5, ge=1, le=1000)

    _lower = field_validator("username", mode="after")(lambda v: v.strip().lower())
    _trim = field_validator("group_name", mode="before")(lambda v: v.strip() if isinstance(v, str) else v)


def _request_out(r: SignupRequest) -> dict[str, Any]:
    return {
        "id": r.id, "full_name": r.full_name, "email": r.email, "group_name": r.group_name, "message": r.message, "status": r.status,
        "created_at": r.created_at, "verified_at": r.verified_at, "decided_at": r.decided_at, "decided_by": r.decided_by,
    }  # fmt: skip


def _settings_out(db: Session) -> dict[str, Any]:
    return {**_settings(db), "mail_enabled": mail.mail_enabled(db), "open": signup_enabled(db)}


@router.get("/api/signup/settings")
def get_signup_settings(_: Principal = Depends(require("users:manage")), db: Session = Depends(get_db)) -> dict[str, Any]:
    return _settings_out(db)


@router.put("/api/signup/settings")
def put_signup_settings(body: SignupSettings, request: Request, _: Principal = Depends(require("users:manage")), db: Session = Depends(get_db)) -> dict[str, Any]:
    if body.enabled and not mail.mail_enabled(db):
        raise HTTPException(422, msg("Sign-up requests need e-mail: set and enable the SMTP server first (Settings → E-mail)"))
    set_setting(db, "signup", body.model_dump())
    audit.record(db, request, "signup.settings", details=body.model_dump())
    db.commit()
    return _settings_out(db)


@router.get("/api/signup/requests")
def list_requests(_: Principal = Depends(require("users:manage")), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """The requests to review first, then the decided ones; unconfirmed addresses older than the link's validity are dropped."""
    limit = datetime.now(UTC) - timedelta(hours=VERIFY_HOURS)
    for r in db.scalars(select(SignupRequest).where(SignupRequest.status == "unverified")):
        if _aware(r.created_at) < limit:
            db.delete(r)
    db.commit()
    order = {"pending": 0, "unverified": 1, "approved": 2, "rejected": 2}
    rows = sorted(db.scalars(select(SignupRequest)), key=lambda r: (order.get(r.status, 3), -_aware(r.created_at).timestamp()))
    return [_request_out(r) for r in rows]


def _reviewable(db: Session, request_id: int) -> SignupRequest:
    r = db.get(SignupRequest, request_id)
    if r is None:
        raise HTTPException(404, msg("Request not found"))
    if r.status != "pending":
        raise HTTPException(409, msg("This request isn't waiting for a decision") if r.status != "unverified" else msg("This address isn't confirmed yet"))
    return r


@router.post("/api/signup/requests/{request_id}/approve")
async def approve_request(request_id: int, body: Approval, request: Request, me: Principal = Depends(require("users:manage")), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Create the group and its manager, and send them the link to set their password."""
    r = _reviewable(db, request_id)
    if db.scalar(select(Group.id).where(func.lower(Group.name) == body.group_name.lower())):
        raise HTTPException(409, msg("A group named '{name}' already exists", name=body.group_name))
    if db.scalar(select(User.id).where(User.username == body.username)):
        raise HTTPException(409, msg("The username '{username}' is taken", username=body.username))
    if db.scalar(select(User.id).where(func.lower(User.email) == r.email)):
        raise HTTPException(409, msg("A user with the address {email} already exists", email=r.email))
    group = Group(name=body.group_name, max_members=body.max_members, notes=f"Requested by {r.full_name} ({r.email})")
    db.add(group)
    db.flush()
    lang = r.language
    user = User(username=body.username, full_name=r.full_name, email=r.email, password_hash="!", group_id=group.id, group_manager=True, language=lang)
    db.add(user)
    db.flush()
    token = secrets.token_urlsafe(32)
    db.add(Invitation(group_id=group.id, email=r.email, full_name=r.full_name, token_hash=_hash(token), user_id=user.id, invited_by=me.display_name, language=lang,
                      expires_at=datetime.now(UTC) + timedelta(days=INVITATION_DAYS)))  # fmt: skip
    r.status, r.decided_at, r.decided_by, r.user_id, r.token_hash = "approved", datetime.now(UTC), me.display_name, user.id, None
    audit.record(db, request, "signup.approved", target=f"user:{user.id}", details={"email": r.email, "group": group.name, "username": user.username})
    await _send(
        db, r.email, lang, tr(lang, "Your EV Charge access is ready"),
        [_hello(lang, r.full_name),
         tr(lang, "Your request was accepted. Your group “{group}” is ready, and your username is {username}.", group=group.name, username=user.username),
         tr(lang, "Choose your password to sign in (link valid for {days} days). You can then add your cars and chargers, and invite up to {n} other member(s).",
            days=INVITATION_DAYS, n=group.max_members - 1)],
        f"{_origin(request)}/invite?token={token}", tr(lang, "Choose my password"),
    )  # fmt: skip
    db.commit()  # after the e-mail: a request isn't approved without its owner being told
    return _request_out(r)


@router.post("/api/signup/requests/{request_id}/reject")
async def reject_request(request_id: int, request: Request, me: Principal = Depends(require("users:manage")), db: Session = Depends(get_db)) -> dict[str, Any]:
    r = _reviewable(db, request_id)
    r.status, r.decided_at, r.decided_by, r.token_hash = "rejected", datetime.now(UTC), me.display_name, None
    audit.record(db, request, "signup.rejected", details={"email": r.email, "group": r.group_name})
    db.commit()
    try:  # a courtesy: the decision stands even if the e-mail can't leave
        text = tr(r.language, "Your request for access to EV Charge wasn't accepted.")
        await mail.send_mail(db, [r.email], tr(r.language, "Your EV Charge request"), f"{_hello(r.language, r.full_name)}\n\n{text}\n\n— EV Charge\n")
    except mail.MailError:
        pass
    return _request_out(r)


@router.delete("/api/signup/requests/{request_id}", status_code=204)
def delete_request(request_id: int, _: Principal = Depends(require("users:manage")), db: Session = Depends(get_db)) -> Response:
    r = db.get(SignupRequest, request_id)
    if r is None:
        raise HTTPException(404, msg("Request not found"))
    db.delete(r)
    db.commit()
    return Response(status_code=204)


# ── Invitations ──────────────────────────────────────────────────────────────


class InvitationIn(BaseModel):
    email: str = Field(max_length=200)
    full_name: str | None = Field(default=None, max_length=100)
    grants: list[CarGrantIO] = Field(default=[], max_length=100)  # the member's role on the group's cars
    group_id: int | None = None  # administrators: another group than their own

    _mail = field_validator("email", mode="after")(_email)


class Acceptance(TokenIn):
    username: str | None = Field(default=None, min_length=2, max_length=50, pattern=USERNAME)
    full_name: str | None = Field(default=None, max_length=100)
    password: str | None = Field(default=None, max_length=200)


def _invitation_out(i: Invitation) -> dict[str, Any]:
    return {
        "id": i.id, "group_id": i.group_id, "group_name": i.group.name, "email": i.email, "full_name": i.full_name, "invited_by": i.invited_by,
        "created_at": i.created_at, "expires_at": i.expires_at, "expired": _aware(i.expires_at) <= datetime.now(UTC),
        "new_group": i.user_id is not None,  # the link sent after an approved sign-up request
        "grants": json.loads(i.grants or "[]"),
    }  # fmt: skip


def _managed_invitation(db: Session, invitation_id: int, me: Principal) -> Invitation:
    i = db.get(Invitation, invitation_id)
    if i is None or i.accepted_at is not None or not (me.has("users:manage") or i.group_id == me.group_id):
        raise HTTPException(404, msg("Invitation not found"))
    return i


async def _send_invitation(db: Session, request: Request, i: Invitation, token: str) -> None:
    lang = i.language
    who = i.invited_by or tr(lang, "A manager")
    await _send(
        db, i.email, lang, tr(lang, "Join “{group}” on EV Charge", group=i.group.name),
        [_hello(lang, i.full_name),
         tr(lang, "{who} invites you to join the group “{group}” on EV Charge, to record and follow charging sessions.", who=who, group=i.group.name),
         tr(lang, "The link is valid for {days} days.", days=INVITATION_DAYS)],
        f"{_origin(request)}/invite?token={token}", tr(lang, "Join the group"),
    )  # fmt: skip


@router.get("/api/invitations")
def list_invitations(me: Principal = Depends(manager), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    """The invitations not accepted yet: of one's group, or of every group for administrators."""
    query = select(Invitation).where(Invitation.accepted_at.is_(None)).order_by(Invitation.created_at.desc())
    if not me.has("users:manage"):
        query = query.where(Invitation.group_id == me.group_id)
    return [_invitation_out(i) for i in db.scalars(query)]


@router.post("/api/invitations", status_code=201)
async def invite(body: InvitationIn, request: Request, me: Principal = Depends(manager), db: Session = Depends(get_db)) -> dict[str, Any]:
    """Invite someone to the group by e-mail, within its member limit."""
    if not mail.mail_enabled(db):
        raise HTTPException(422, msg("Invitations need e-mail: an administrator must set the SMTP server first (Settings → E-mail)"))
    group_id = body.group_id if body.group_id is not None and me.has("users:manage") else me.group_id
    group = db.get(Group, group_id) if group_id is not None else None
    if group is None:
        raise HTTPException(422, msg("Unknown group"))
    if db.scalar(select(User.id).where(func.lower(User.email) == body.email)):
        raise HTTPException(409, msg("A user with the address {email} already exists", email=body.email))
    for g in body.grants:
        car = db.get(Car, g.car_id)
        if car is None or car.group_id != group.id or db.get(Role, g.role_id) is None:
            raise HTTPException(422, msg("Unknown car or role"))
    previous = next((i for i in _pending_invitations(db, group.id) if i.email == body.email and i.user_id is None), None)
    if previous is None:
        _check_room(db, group)
    token = secrets.token_urlsafe(32)
    i = previous or Invitation(group_id=group.id, email=body.email)
    i.full_name, i.grants, i.token_hash = (body.full_name or "").strip() or None, json.dumps([g.model_dump() for g in body.grants]), _hash(token)
    i.invited_by, i.created_at, i.expires_at = me.display_name, datetime.now(UTC), datetime.now(UTC) + timedelta(days=INVITATION_DAYS)
    i.language = i18n.language_of(request, me.user)  # the inviter's: the best guess for someone we know nothing of yet
    db.add(i)
    db.flush()
    audit.record(db, request, "invitation.sent", details={"email": i.email, "group": group.name})
    await _send_invitation(db, request, i, token)
    db.commit()
    return _invitation_out(i)


@router.post("/api/invitations/{invitation_id}/resend")
async def resend_invitation(invitation_id: int, request: Request, me: Principal = Depends(manager), db: Session = Depends(get_db)) -> dict[str, Any]:
    """A new link (the previous one stops working), valid for the full time again."""
    i = _managed_invitation(db, invitation_id, me)
    token = secrets.token_urlsafe(32)
    i.token_hash, i.expires_at = _hash(token), datetime.now(UTC) + timedelta(days=INVITATION_DAYS)
    if i.user_id is None:
        await _send_invitation(db, request, i, token)
    else:
        lang = i.language
        lines = [_hello(lang, i.full_name), tr(lang, "Here is a new link to choose your password for the group “{group}”.", group=i.group.name)]
        await _send(db, i.email, lang, tr(lang, "Your EV Charge access is ready"), lines, f"{_origin(request)}/invite?token={token}", tr(lang, "Choose my password"))
    db.commit()
    return _invitation_out(i)


@router.delete("/api/invitations/{invitation_id}", status_code=204)
def revoke_invitation(invitation_id: int, request: Request, me: Principal = Depends(manager), db: Session = Depends(get_db)) -> Response:
    i = _managed_invitation(db, invitation_id, me)
    audit.record(db, request, "invitation.revoked", details={"email": i.email, "group": i.group.name})
    db.delete(i)
    db.commit()
    return Response(status_code=204)


def _open(db: Session, token: str, lang: str | None = None) -> Invitation:
    i = db.scalar(select(Invitation).where(Invitation.token_hash == _hash(token)))
    if i is None or i.accepted_at is not None:
        raise HTTPException(404, tr(lang, "This invitation isn't valid (already used, replaced by a newer one, or withdrawn)"))
    if _aware(i.expires_at) <= datetime.now(UTC):
        raise HTTPException(410, tr(lang, "This invitation has expired: ask for a new one"))
    return i


@router.get("/api/invitations/open")
def open_invitation(token: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    """What the invitation page shows (public: the token is the key)."""
    i = _open(db, token, i18n.language_of(request))
    user = db.get(User, i.user_id) if i.user_id else None
    return {
        "group_name": i.group.name, "email": i.email, "full_name": i.full_name, "invited_by": i.invited_by,
        "username": user.username if user else None,  # set: the account exists, only a password is asked
        "password_login": local_login_allowed(db),  # else: sign in with single sign-on, no password to choose
    }  # fmt: skip


@router.post("/api/invitations/accept", response_model=Me)
def accept_invitation(body: Acceptance, request: Request, response: Response, db: Session = Depends(get_db)) -> Me:
    """Create the member's account (or set the password of the account made for an approved request) and sign them in."""
    lang = i18n.language_of(request)
    i = _open(db, body.token, lang)
    with_password = local_login_allowed(db)
    if with_password:
        if not body.password or len(body.password) < 8:
            raise HTTPException(422, tr(lang, "Choose a password of at least 8 characters"))
        check_password_strength(body.password)
    if i.user_id is not None:
        user = db.get(User, i.user_id)
        if user is None or not user.active:
            raise HTTPException(404, tr(lang, "This account no longer exists"))
    else:
        if not body.username:
            raise HTTPException(422, tr(lang, "Choose a username"))
        username = body.username.strip().lower()
        if db.scalar(select(User.id).where(User.username == username)):
            raise HTTPException(409, tr(lang, "The username '{username}' is taken: choose another one", username=username))
        if db.scalar(select(User.id).where(func.lower(User.email) == i.email)):
            raise HTTPException(409, tr(lang, "An account with this e-mail address already exists: sign in instead"))
        user = User(username=username, full_name=(body.full_name or i.full_name or "").strip() or None, email=i.email, password_hash="!", group_id=i.group_id)
        db.add(user)
        db.flush()
        check_group_size(db, user)  # the limit may have been lowered since the invitation
        for g in json.loads(i.grants or "[]"):
            car = db.get(Car, g.get("car_id"))
            if car is not None and car.group_id == i.group_id and db.get(Role, g.get("role_id")) is not None:
                db.add(CarGrant(user_id=user.id, car_id=car.id, role_id=g["role_id"]))
    if with_password:
        user.password_hash = hash_password(body.password)  # type: ignore[arg-type]
    i.accepted_at, i.user_id = datetime.now(UTC), user.id
    user.language = i18n.valid(request.cookies.get(i18n.COOKIE)) or user.language or i18n.valid(i.language)  # the one chosen on the page, else the e-mail's
    audit.record(db, request, "invitation.accepted", target=f"user:{user.id}", user=user, details={"group": i.group.name})
    db.commit()
    db.refresh(user)
    if with_password:
        start_session(db, response, user, request, method="password")
    return me_out(principal_for(user))
