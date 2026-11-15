"""Languages of the interface and of the e-mails.

Texts are written in English in the code and looked up, as they are, in one catalogue per language
(locales/<language>.json: {"English text": "translation"}); a text without a translation stays in English.
Values go in with {name} placeholders: translate("fr", "{n} session(s)", n=3).

A person's language is the one of their profile, else the one chosen on the sign-in page (a cookie), else
the first language of their browser that we have, else English.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import Request
from markupsafe import Markup, escape

LANGUAGES: dict[str, str] = {"en": "English", "fr": "Français", "nl": "Nederlands", "de": "Deutsch"}
DEFAULT = "en"
COOKIE = "ev-lang"
_DIR = Path(__file__).parent / "locales"


def _load() -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {DEFAULT: {}}
    for code in LANGUAGES:
        path = _DIR / f"{code}.json"
        if code != DEFAULT and path.exists():
            out[code] = json.loads(path.read_text(encoding="utf-8"))
    return out


CATALOGUES = _load()
VERSION = hashlib.sha256(json.dumps(CATALOGUES, sort_keys=True).encode()).hexdigest()[:12]  # changes with the translations: browsers refetch them


def valid(code: str | None) -> str | None:
    return code if code in LANGUAGES else None


def translate(language: str | None, text: str, **values: Any) -> str:
    out = CATALOGUES.get(language or DEFAULT, {}).get(text, text)
    for name, value in values.items():
        out = out.replace("{" + name + "}", str(value))
    return out


def translator(language: str):  # type: ignore[no-untyped-def]
    """t("…") for the templates: the catalogue's text as markup (it may hold <b>…</b>), the values escaped."""

    def t(text: str, **values: Any) -> Markup:
        return Markup(translate(language, text, **{k: escape(v) for k, v in values.items()}))

    return t


def from_browser(header: str | None) -> str | None:
    """The first language of an Accept-Language header that we have ("fr-BE,fr;q=0.9,en;q=0.8" → "fr")."""
    wanted = []
    for part in (header or "").split(","):
        code, _, q = part.strip().partition(";q=")
        try:
            wanted.append((-(float(q) if q else 1.0), len(wanted), code.split("-")[0].lower()))
        except ValueError:
            continue
    return next((code for _q, _n, code in sorted(wanted) if code in LANGUAGES), None)


def language_of(request: Request, user: Any = None) -> str:
    return valid(getattr(user, "language", None)) or valid(request.cookies.get(COOKIE)) or from_browser(request.headers.get("accept-language")) or DEFAULT


class Message(str):
    """A text of the server (an error message…) that is translated when it is sent: English as a plain string, with what is needed to translate it."""

    key: str
    values: dict[str, Any]

    def __new__(cls, text: str, **values: Any) -> Message:
        # A list is shown as "a, b"; str(exception) may be a message itself
        values = {k: v if isinstance(v, (str, int, float)) else [str(i) for i in v] if isinstance(v, list) else str(v) for k, v in values.items()}
        self = super().__new__(cls, translate(None, text, **{k: ", ".join(v) if isinstance(v, list) else v for k, v in values.items()}))
        self.key, self.values = text, values
        return self

    def __str__(self) -> str:  # str(exception) keeps the message translatable
        return self


msg = Message


def localize(language: str | None, value: Any) -> Any:
    """The messages of a value (a message, or the lists and dicts of a response that hold some) in a language."""
    if isinstance(value, Message):
        values = {k: localize(language, v) for k, v in value.values.items()}
        return translate(language, value.key, **{k: ", ".join(v) if isinstance(v, list) else v for k, v in values.items()})
    if isinstance(value, list):
        return [localize(language, v) for v in value]
    if isinstance(value, dict):
        return {k: localize(language, v) for k, v in value.items()}
    return value


_STORED = "\x1ei18n:"  # in front of a message kept in the database with what is needed to translate it later


def store(message: str, limit: int = 2000) -> str:
    """A message to keep (the last error of a charger…): read back with stored(), in the language of who reads it."""

    def pack(value: Any) -> Any:
        if isinstance(value, list):
            return [pack(v) for v in value]
        return {"k": value.key, "v": {k: pack(v) for k, v in value.values.items()}} if isinstance(value, Message) else value

    packed = _STORED + json.dumps(pack(message), ensure_ascii=False)
    return packed if isinstance(message, Message) and len(packed) <= limit else str.__str__(message)[:limit]


def stored(language: str | None, text: str | None) -> str | None:
    def unpack(value: Any) -> Any:
        if isinstance(value, list):
            return [unpack(v) for v in value]
        return Message(value["k"], **{k: unpack(v) for k, v in value["v"].items()}) if isinstance(value, dict) else value

    if not text or not text.startswith(_STORED):
        return text
    try:
        return localize(language, unpack(json.loads(text[len(_STORED) :])))
    except (ValueError, KeyError, TypeError):
        return text[len(_STORED) :]


def catalogue_script(language: str) -> str:
    """The catalogue for the pages' scripts (EV.t): loaded before them, cached by the browser."""
    return f"window.EV=Object.assign(window.EV||{{}},{{lang:{json.dumps(language)},T:{json.dumps(CATALOGUES.get(language, {}), ensure_ascii=False)}}});"
