"""Languages: every text used has its translations, and each person gets their language."""

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ev_charge import i18n
from ev_charge.connectors import CONNECTORS
from ev_charge.main import app
from ev_charge.permissions import ALL_PERMISSIONS

SRC = Path(i18n.__file__).parent
STRING = r"""("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')"""
# t("…") in templates, EV.t('…') and T('…') in scripts, tr(language, "…") and msg("…") in Python
CALLS = [re.compile(r"(?<![\w.])(?:EV\.t|T|t|_?msg)\(\s*" + STRING), re.compile(r"\btr\(\s*[\w.]+\s*,\s*" + STRING)]


def used_texts() -> dict[str, str]:
    """Every text passed to a translation function, with a file it is used in."""
    found: dict[str, str] = {}
    for path in SRC.rglob("*"):
        if path.suffix not in (".html", ".js", ".py") or "vendor" in path.parts or "migrations" in path.parts or path.name == "i18n.py":
            continue
        source = path.read_text(encoding="utf-8")
        for call in CALLS:
            for m in call.finditer(source):
                found.setdefault(m.group(1)[1:-1].replace("\\'", "'").replace('\\"', '"'), path.name)
    for label, _group in ALL_PERMISSIONS.values():  # shown through EV.t(label)
        found.setdefault(label, "permissions.py")
    for connector in CONNECTORS.values():  # the connection form of a charger: EV.t(label)
        for label in (connector.label, *(field[1] for field in connector.fields)):
            found.setdefault(label, "connectors")
    for _label, group in ALL_PERMISSIONS.values():  # the titles of the lists of permissions: EV.t(group)
        found.setdefault(group, "permissions.py")
    return found


@pytest.mark.parametrize("language", [code for code in i18n.LANGUAGES if code != i18n.DEFAULT])
def test_every_text_is_translated(language):
    catalogue = i18n.CATALOGUES[language]
    used = used_texts()
    assert len(used) > 100  # the search above still finds the texts
    missing = sorted(f"{text!r} ({where})" for text, where in used.items() if text not in catalogue)
    assert not missing, f"{language}: no translation for {missing}"
    unused = sorted(set(catalogue) - set(used))
    assert not unused, f"{language}: translations of texts no longer used: {unused}"
    for text, translation in catalogue.items():
        assert translation.strip(), text
        assert set(re.findall(r"\{\w+\}", text)) == set(re.findall(r"\{\w+\}", translation)), f"{language}: the values of {text!r} differ in its translation"
        assert re.findall(r"</?\w+>", text) == re.findall(r"</?\w+>", translation), f"{language}: the markup of {text!r} differs in its translation"


def test_translate_and_browser_language():
    assert i18n.translate("fr", "Sign in") == "Se connecter" and i18n.translate("fr", "Not in the catalogue") == "Not in the catalogue"
    assert i18n.translate("de", "Sign in with {name}", name="Authelia") == "Mit Authelia anmelden"
    assert i18n.translate(None, "Hello {name},", name="Ada") == "Hello Ada," and i18n.translate("xx", "Sign in") == "Sign in"
    assert str(i18n.translator("fr")("Sign in with {name}", name="<b>x</b>")) == "Se connecter avec &lt;b&gt;x&lt;/b&gt;"  # values are escaped in pages
    assert i18n.from_browser("fr-BE,fr;q=0.9,en;q=0.8") == "fr" and i18n.from_browser("es,nl;q=0.5,de;q=0.8") == "de"
    assert i18n.from_browser("es-ES,pt;q=0.5") is None and i18n.from_browser(None) is None and i18n.from_browser("fr;q=abc,nl") == "nl"


def test_public_pages_follow_the_browser_then_the_choice(anon):
    assert "Create the first <b>administrator</b>" in anon.get("/setup").text  # English by default
    french = anon.get("/setup", headers={"Accept-Language": "fr-BE,fr;q=0.9,en;q=0.8"})
    assert '<html lang="fr">' in french.text and "Créez le premier compte <b>administrateur</b>" in french.text and "/i18n/fr.js?v=" in french.text
    assert '<option value="fr" selected>Français</option>' in french.text
    # Chosen on the page: remembered by the browser, before the browser's own language
    assert anon.put("/api/auth/language", json={"language": "xx"}).status_code == 422
    assert anon.put("/api/auth/language", json={"language": "nl"}).json() == {"language": "nl"}
    assert "Maak het eerste <b>beheerders</b>account aan" in anon.get("/setup", headers={"Accept-Language": "fr"}).text
    assert anon.put("/api/auth/language", json={"language": None}).status_code == 200
    assert '<html lang="de">' in anon.get("/setup", headers={"Accept-Language": "de-DE"}).text

    script = anon.get("/i18n/fr.js")
    assert script.status_code == 200 and "immutable" in script.headers["cache-control"] and '"Sign in":"Se connecter"' in script.text.replace('": "', '":"')
    assert json.loads(script.text[script.text.index("T:") + 2 : -3])["Cancel"] == "Annuler"
    assert anon.get("/i18n/en.js").text.endswith("T:{}});") and anon.get("/i18n/xx.js").status_code == 404


def test_language_of_the_profile(client):
    assert client.get("/api/auth/me").json()["language"] is None and "My account" in client.get("/account").text
    assert client.put("/api/auth/language", json={"language": "fr"}).status_code == 200
    assert client.get("/api/auth/me").json()["language"] == "fr"
    page = client.get("/account", headers={"Accept-Language": "de"})  # the profile comes before the browser
    assert '<html lang="fr">' in page.text and "Mon compte" in page.text and "Tableau de bord" in page.text and '<option value="fr" selected>' in page.text
    # Another browser of the same person gets it too, and the sign-in page keeps it after signing out
    other = TestClient(app)
    other.post("/api/auth/login", json={"username": "admin", "password": "admin-password"})
    assert "Tableau de bord" in other.get("/").text
    client.post("/api/auth/logout")
    assert "Connectez-vous pour enregistrer" in client.get("/login").text
    other.put("/api/auth/language", json={"language": None})
    assert other.get("/api/auth/me").json()["language"] is None and "Dashboard" in other.get("/").text
    other.close()


def test_messages_of_the_server_in_the_language_of_the_person(client, anon):
    assert client.get("/api/cars/999").json() == {"detail": "Car not found"}  # English by default
    assert client.get("/api/cars/999", headers={"Accept-Language": "nl"}).json() == {"detail": "Auto niet gevonden"}
    assert anon.post("/api/auth/login", json={"username": "x", "password": "y"}, headers={"Accept-Language": "de"}).json() == {"detail": "Benutzername oder Passwort falsch"}
    client.put("/api/auth/language", json={"language": "fr"})
    assert client.get("/api/cars/999", headers={"Accept-Language": "nl"}).json() == {"detail": "Voiture introuvable"}  # the profile first
    client.post("/api/cars", json={"name": "Ioniq 5"})
    assert client.post("/api/cars", json={"name": "Ioniq 5"}).json() == {"detail": "Une voiture nommée 'Ioniq 5' existe déjà"}  # with its values
    # A message raised deep in the code (reading a document) is translated when it is sent
    upload = client.post("/api/reimbursement-documents/preview", files={"file": ("x.csv", b"", "text/csv")}, data={"charger_id": "1"})
    assert upload.status_code == 422 and upload.json() == {"detail": "Le fichier est vide."}
    # The rows of an import, and the exported statement
    preview = client.post("/api/import/sessions/preview", json={"rows": [["2026-01-05", "abc"]], "mapping": {"charge_date": 0, "kwh": 1}, "car_id": 1}).json()
    assert preview["rows"][0]["errors"] == ['kWh : impossible de lire "abc"']
    assert client.get("/api/reports/reimbursement?fmt=csv").text.splitlines()[0].startswith("\ufeffDate;Voiture;Plaque;Compteur (km);Lieu;Chargeur;Remboursé par;")


def test_emails_in_the_language_of_the_recipient(client, mailbox):
    client.put("/api/signup/settings", json={"enabled": True})
    visitor = TestClient(app, base_url="https://ev.example.com", headers={"Accept-Language": "fr-BE"})
    form = {"full_name": "Greg Collègue", "email": "greg@example.org", "group_name": "Collègues"}
    assert visitor.post("/api/signup", json=form).json()["detail"].startswith("Consultez votre boîte mail")
    to, subject, text = mailbox.messages[-1]
    assert (
        subject == "Confirmez votre adresse e-mail"
        and "Bonjour Greg Collègue," in text
        and "« Collègues »" in text
        and "Confirmer mon adresse: https://ev.example.com/signup/verify" in text
    )
    assert visitor.post("/api/signup/verify", json={"token": "x" * 30}).json()["detail"].startswith("Ce lien n'est pas valide")
    visitor.post("/api/signup/verify", json={"token": mailbox.token()})
    # Approved by an administrator working in English: the e-mail is still in the requester's language
    (req,) = client.get("/api/signup/requests").json()
    client.post(f"/api/signup/requests/{req['id']}/approve", json={"username": "greg", "group_name": "Collègues"})
    to, subject, text = mailbox.messages[-1]
    assert subject == "Votre accès à EV Charge est prêt" and "votre nom d'utilisateur est greg" in text
    me = visitor.post("/api/invitations/accept", json={"token": mailbox.token(), "password": "member-password"}).json()
    assert me["language"] == "fr"  # their account starts in their language

    # An invitation is written in the inviter's language; the invited person's own choice on the page wins for their account
    visitor.post("/api/invitations", json={"email": "mia@example.org"})
    assert mailbox.messages[-1][1] == "Rejoignez « Collègues » sur EV Charge"
    invited = TestClient(app)
    invited.put("/api/auth/language", json={"language": "de"})
    assert (
        invited.post("/api/invitations/accept", json={"token": mailbox.token(), "username": "greg", "password": "member-password"})
        .json()["detail"]
        .startswith("Der Benutzername „greg“")
    )
    assert invited.post("/api/invitations/accept", json={"token": mailbox.token(), "username": "mia", "password": "member-password"}).json()["language"] == "de"
    for c in (visitor, invited):
        c.close()
