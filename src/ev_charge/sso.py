"""Single sign-on with OpenID Connect (Authelia, Keycloak, Entra ID, Google…).

Authorization code flow with PKCE, state and nonce. The ID token comes straight from the provider's
token endpoint over TLS, so (as OpenID Connect allows) its signature is not checked; its issuer,
audience, expiry and nonce are. The user is found by (issuer, subject), else by username or e-mail
(the account is then linked), else created when auto-creation is on. Provider groups can set the
global role (first matching mapping wins).

Settings live in the settings table ("sso"); the client secret is encrypted (secretbox).
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import CONFIG
from .models import Role, User
from .pricing import get_setting, set_setting
from .secretbox import decrypt_json, encrypt_json

TIMEOUT = aiohttp.ClientTimeout(total=15)
FLOW_SECONDS = 600  # time allowed between the redirect to the provider and the callback
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "name": "Authelia",  # shown on the sign-in button
    "issuer": "",  # e.g. https://auth.example.com
    "client_id": "",
    "scopes": "openid profile email groups",
    "auth_method": "client_secret_basic",  # or client_secret_post
    "redirect_uri": "",  # empty: {this site}/auth/sso/callback
    "username_claim": "preferred_username",
    "groups_claim": "groups",
    "auto_create": True,
    "default_role_id": None,  # global role of new users (None: no global role, cars given by an administrator)
    "group_roles": [],  # [{"group": "ev-admins", "role_id": 1}, …] first match wins, applied at each sign-in
    "local_login": True,  # allow username/password sign-in next to single sign-on
}


class SsoError(Exception):
    """Shown to the user on the sign-in page."""


def get_config(db: Session) -> dict[str, Any]:
    return {**DEFAULTS, **get_setting(db, "sso", {})}


def get_secret(db: Session) -> str:
    return decrypt_json(get_setting(db, "sso_secret", None)).get("client_secret", "")


def save_config(db: Session, cfg: dict[str, Any], client_secret: str | None) -> None:
    set_setting(db, "sso", {k: cfg.get(k, v) for k, v in DEFAULTS.items()})
    if client_secret:
        set_setting(db, "sso_secret", encrypt_json({"client_secret": client_secret}))


def sso_enabled(db: Session) -> bool:
    cfg = get_config(db)
    return bool(cfg["enabled"] and cfg["issuer"] and cfg["client_id"])


def local_login_allowed(db: Session) -> bool:
    """Password sign-in: always when single sign-on is off; config.json allow_local_login re-opens it (lock-out escape)."""
    return CONFIG.allow_local_login or not sso_enabled(db) or bool(get_config(db)["local_login"])


# ── Provider ─────────────────────────────────────────────────────────────────

_discovery: dict[str, tuple[float, dict[str, Any]]] = {}


async def discover(issuer: str) -> dict[str, Any]:
    issuer = issuer.rstrip("/")
    cached = _discovery.get(issuer)
    if cached and time.monotonic() - cached[0] < 3600:
        return cached[1]
    url = f"{issuer}/.well-known/openid-configuration"
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT, trust_env=True) as http, http.get(url) as resp:
            if resp.status != 200:
                raise SsoError(f"The identity provider's discovery document answered HTTP {resp.status} ({url})")
            doc = await resp.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise SsoError(f"Cannot reach the identity provider ({exc})") from exc
    for key in ("authorization_endpoint", "token_endpoint", "issuer"):
        if not doc.get(key):
            raise SsoError(f"The discovery document has no {key}")
    _discovery[issuer] = (time.monotonic(), doc)
    return doc


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


async def begin(db: Session, redirect_uri: str, next_url: str) -> tuple[str, str]:
    """The provider URL to redirect to, and the encrypted flow state to keep in a cookie."""
    cfg = get_config(db)
    if not sso_enabled(db):
        raise SsoError("Single sign-on is not enabled")
    doc = await discover(cfg["issuer"])
    state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "scope": cfg["scopes"] or "openid",
        "state": state,
        "nonce": nonce,
        "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
        "code_challenge_method": "S256",
    }
    flow = encrypt_json({"state": state, "nonce": nonce, "verifier": verifier, "next": next_url, "redirect_uri": redirect_uri, "at": time.time()})
    return f"{doc['authorization_endpoint']}?{urlencode(params)}", flow


def _claims(id_token: str) -> dict[str, Any]:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise SsoError("The identity provider returned an unreadable ID token") from exc


async def finish(db: Session, flow_cookie: str | None, state: str, code: str) -> tuple[dict[str, Any], str]:
    """Exchange the code; return the user's claims (ID token + userinfo) and the page to go back to."""
    if not flow_cookie:
        raise SsoError("The sign-in took too long or cookies are blocked: try again")
    try:
        flow = decrypt_json(flow_cookie)
    except Exception as exc:
        raise SsoError("Invalid sign-in state: try again") from exc
    if not secrets.compare_digest(flow.get("state", ""), state or ""):
        raise SsoError("Invalid sign-in state: try again")
    if time.time() - flow.get("at", 0) > FLOW_SECONDS:
        raise SsoError("The sign-in took too long: try again")
    cfg = get_config(db)
    doc = await discover(cfg["issuer"])
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": flow["redirect_uri"], "code_verifier": flow["verifier"]}
    secret = get_secret(db)
    auth = None
    if cfg["auth_method"] == "client_secret_post":
        data |= {"client_id": cfg["client_id"], "client_secret": secret}
    else:
        auth = aiohttp.BasicAuth(cfg["client_id"], secret)
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT, trust_env=True) as http:
            async with http.post(doc["token_endpoint"], data=data, auth=auth) as resp:
                tokens = await resp.json(content_type=None)
                if resp.status != 200:
                    raise SsoError(f"The identity provider refused the sign-in: {tokens.get('error_description') or tokens.get('error') or resp.status}")
            claims = _claims(tokens.get("id_token", ""))
            if doc.get("userinfo_endpoint") and tokens.get("access_token"):
                async with http.get(doc["userinfo_endpoint"], headers={"Authorization": f"Bearer {tokens['access_token']}"}) as resp:
                    if resp.status == 200:
                        info = await resp.json(content_type=None)
                        if info.get("sub") == claims.get("sub"):
                            claims = {**info, **claims}
    except aiohttp.ClientError as exc:
        raise SsoError(f"Cannot reach the identity provider ({exc})") from exc
    if claims.get("iss", "").rstrip("/") != doc["issuer"].rstrip("/"):
        raise SsoError("The ID token comes from another issuer")
    aud = claims.get("aud")
    if cfg["client_id"] not in (aud if isinstance(aud, list) else [aud]):
        raise SsoError("The ID token is not meant for this application")
    if float(claims.get("exp", 0)) < time.time() - 60:
        raise SsoError("The ID token has expired")
    if claims.get("nonce") != flow["nonce"]:
        raise SsoError("Invalid sign-in (nonce): try again")
    if not claims.get("sub"):
        raise SsoError("The ID token has no subject")
    return claims, flow.get("next") or "/"


def _clean_username(value: str) -> str:
    return re.sub(r"[^a-z0-9._@-]", "", value.strip().lower())[:50]


def resolve_user(db: Session, claims: dict[str, Any]) -> tuple[User, str]:
    """The user for these claims (linked or created), with the role from the group mappings. Returns (user, how)."""
    cfg = get_config(db)
    issuer = claims["iss"].rstrip("/")
    user = db.scalar(select(User).where(User.oidc_issuer == issuer, User.oidc_subject == str(claims["sub"])))
    how = "existing"
    username = _clean_username(str(claims.get(cfg["username_claim"]) or claims.get("preferred_username") or claims.get("email") or ""))
    email = (claims.get("email") or "").strip().lower() or None
    if user is None:
        user = db.scalar(select(User).where(User.username == username)) if len(username) >= 2 else None
        if user is None and email:
            user = db.scalar(select(User).where(func.lower(User.email) == email))
        if user is not None:
            if user.oidc_subject and user.oidc_issuer == issuer:
                raise SsoError("This EV Charge account is linked to another single sign-on account")
            how = "linked"
        elif cfg["auto_create"]:
            if len(username) < 2:
                raise SsoError(f"The identity provider sent no usable username (claim '{cfg['username_claim']}')")
            role = db.get(Role, cfg["default_role_id"]) if cfg["default_role_id"] else None
            user = User(username=username, full_name=claims.get("name"), email=email, role_id=role.id if role else None, password_hash="!", active=True)
            db.add(user)
            how = "created"
        else:
            raise SsoError("No EV Charge account for you yet: ask an administrator to create it")
        user.oidc_issuer, user.oidc_subject = issuer, str(claims["sub"])
    if not user.active:
        raise SsoError("Your EV Charge account is disabled")
    if claims.get("name") and not user.full_name:
        user.full_name = claims["name"]
    if email and not user.email:
        user.email = email
    groups = claims.get(cfg["groups_claim"]) or []
    groups = [groups] if isinstance(groups, str) else list(groups)
    for mapping in cfg["group_roles"] or []:
        if mapping.get("group") in groups and db.get(Role, mapping.get("role_id")) is not None:
            user.role_id = mapping["role_id"]
            break
    db.flush()
    return user, how
