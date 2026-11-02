"""E-mail settings and sending, against a local SMTP server with a login (aiosmtpd)."""

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword


class Inbox:
    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802
        self.messages.append((envelope.mail_from, envelope.rcpt_tos, envelope.content.decode()))
        return "250 OK"


def _auth(server, session, envelope, mechanism, auth_data):
    ok = isinstance(auth_data, LoginPassword) and auth_data.login == b"mailer" and auth_data.password == b"pw"
    return AuthResult(success=ok, handled=False)  # handled=False: the server answers 535 on failure


@pytest.fixture()
def smtp():
    import socket

    with socket.socket() as s:  # a free port (the controller can't wait on port 0)
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    inbox = Inbox()
    inbox.port = port
    controller = Controller(inbox, hostname="127.0.0.1", port=port, authenticator=_auth, auth_require_tls=False)
    controller.start()
    yield inbox
    controller.stop()


def _settings(smtp, **kw):
    return {"enabled": True, "host": "127.0.0.1", "port": smtp.port, "security": "none", "username": "mailer", "password": "pw",
            "from_address": "ev@example.com", "from_name": "EV Charge"} | kw  # fmt: skip


def test_save_and_test_mail(client, smtp):
    assert client.put("/api/settings/email", json={"enabled": True, "host": ""}).status_code == 422
    r = client.put("/api/settings/email", json=_settings(smtp))
    assert r.status_code == 200 and r.json()["has_password"] and "password" not in r.json()
    t = client.post("/api/settings/email/test", json=_settings(smtp, password="") | {"to": "me@example.com"})  # empty: the stored password
    assert t.status_code == 200, t.text
    sender, rcpt, content = smtp.messages[-1]
    assert sender == "ev@example.com" and rcpt == ["me@example.com"] and "If you read this, e-mail works" in content
    assert "From: EV Charge <ev@example.com>" in content
    bad = client.post("/api/settings/email/test", json=_settings(smtp, password="wrong") | {"to": "me@example.com"})
    assert bad.status_code == 422 and "refused the login" in bad.json()["detail"]
    off = client.post("/api/settings/email/test", json=_settings(smtp, port=1) | {"to": "me@example.com"})
    assert off.status_code == 422 and "Cannot reach" in off.json()["detail"]
    events = [e["action"] for e in client.get("/api/audit?action=settings.").json()["items"]]
    assert events.count("settings.email_test") == 3 and "settings.email" in events


def test_mail_settings_need_permission(client, car, make_driver):
    dave = make_driver([car["id"]])
    assert dave.get("/api/settings/email").status_code == 403
    assert dave.post("/api/settings/email/test", json={"to": "x@example.com"}).status_code == 403


def test_empty_fields_from_the_form(client, smtp):
    # The page sends empty inputs as null (no login, no sender name)
    body = _settings(smtp, username=None, password=None, from_name=None) | {"to": "me@example.com"}
    smtp_no_auth = client.post("/api/settings/email/test", json=body)
    assert smtp_no_auth.status_code in (200, 422) and "Input should be" not in smtp_no_auth.text
    assert client.put("/api/settings/email", json=_settings(smtp, username=None, from_name=None)).status_code == 200
