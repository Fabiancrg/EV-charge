"""Read the home charging reimbursement tariffs published by the CREG.

The CREG page "Tarif de remboursement pour la recharge à domicile des voitures de société"
(also in Dutch) holds a table with one row per quarter ("Q4/2026", or "Q3 2026 (01/07-30/09/2026)"
in the table of previous quarters) and one column per region (Wallonie / Bruxelles / Flandre),
in c€/kWh with a decimal comma. These are the maximum fixed amounts per kWh set by the
SPF Finances (circular 2024/C/77 and its quarterly updates).

Columns are found by their header names (French, Dutch or English), so the parser follows
the page if columns are reordered. Nothing is stored here: the Tariffs page shows a preview
and the administrator chooses what to apply.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser

from .i18n import msg

DEFAULT_URL = "https://www.creg.be/fr/consommateurs/prix-et-tarifs/tarif-de-remboursement-pour-la-recharge-a-domicile-des-voitures-de-0"
REGIONS = {"wallonia": "Wallonia", "brussels": "Brussels", "flanders": "Flanders"}
_REGION_WORDS = {
    "wallonia": ("wallon", "walloni"),  # Wallonie, Wallonië, Wallonia, Région wallonne
    "brussels": ("brux", "brussel"),  # Bruxelles, Brussel, Brussels
    "flanders": ("fland", "vlaander", "vlaams"),  # Flandre, Vlaanderen, Flanders
}
_QUARTER_RE = re.compile(r"\bQ\s*([1-4])\s*[/\-\s]\s*(20\d{2})\b", re.IGNORECASE)
MAX_PAGE_BYTES = 2_000_000


class CregError(Exception):
    """The CREG page can't be fetched or holds no readable tariff table."""


@dataclass(frozen=True)
class QuarterRate:
    year: int
    quarter: int
    rates: dict[str, float]  # region → €/kWh

    @property
    def key(self) -> str:
        return f"{self.year}-Q{self.quarter}"

    @property
    def valid_from(self) -> date:
        return date(self.year, 3 * self.quarter - 2, 1)

    @property
    def valid_to(self) -> date:
        nxt = date(self.year + 1, 1, 1) if self.quarter == 4 else date(self.year, 3 * self.quarter + 1, 1)
        return nxt - timedelta(days=1)


def _norm(text: str) -> str:
    return unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode().lower()


class _Tables(HTMLParser):
    """Collects every <table> as rows of cell texts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._depth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self.tables.append([])
        elif self._depth == 1 and tag == "tr":
            self._row = []
        elif self._depth == 1 and tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._depth = max(0, self._depth - 1)
        elif self._depth == 1 and tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif self._depth == 1 and tag == "tr" and self._row is not None:
            if self._row:
                self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _cents(text: str) -> float | None:
    """'37,79' (c€/kWh) → 0.3779 €/kWh; None when the cell holds no number."""
    m = re.search(r"\d+(?:[.,]\d+)?", text.replace(" ", " "))
    if not m:
        return None
    value = float(m.group().replace(",", "."))
    return round(value / 100, 6) if 1 <= value <= 200 else None  # plausible range in c€/kWh


def parse_creg(html: str) -> list[QuarterRate]:
    """Every quarter found in the page's tables, newest first. The first table that gives a quarter wins."""
    parser = _Tables()
    parser.feed(html)
    found: dict[tuple[int, int], QuarterRate] = {}
    for table in parser.tables:
        columns: dict[str, int] = {}
        for row in table:
            if not columns:  # look for the header row
                for i, cell in enumerate(row):
                    for region, words in _REGION_WORDS.items():
                        if region not in columns and any(w in _norm(cell) for w in words):
                            columns[region] = i
                continue
            m = _QUARTER_RE.search(row[0]) if row else None
            if not m:
                continue
            rates = {r: v for r, i in columns.items() if i < len(row) and (v := _cents(row[i])) is not None}
            key = (int(m.group(2)), int(m.group(1)))
            if rates and key not in found:
                found[key] = QuarterRate(year=key[0], quarter=key[1], rates=rates)
    if not found:
        raise CregError(msg("No quarterly tariff table found on the CREG page: its layout may have changed. Enter the tariff by hand."))
    return sorted(found.values(), key=lambda q: (q.year, q.quarter), reverse=True)


def fetch_page(url: str = DEFAULT_URL, timeout: float = 20) -> str:
    """Download the CREG page (honours the HTTPS_PROXY / HTTP_PROXY environment variables)."""
    request = urllib.request.Request(url, headers={"User-Agent": "EV-Charge tariff import (+https://github.com/Fabiancrg/EV-charge)", "Accept-Language": "fr,nl;q=0.8,en;q=0.5"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https URL from the config)
            raw = response.read(MAX_PAGE_BYTES + 1)
            charset = response.headers.get_content_charset() or "utf-8"
    except OSError as exc:
        reason = getattr(exc, "reason", None) or exc
        raise CregError(msg("Cannot reach the CREG website ({reason}). Check this server's internet access, or upload the page saved from your browser.", reason=reason)) from exc
    if len(raw) > MAX_PAGE_BYTES:
        raise CregError(msg("The CREG page is unexpectedly large"))
    return raw.decode(charset, errors="replace")
