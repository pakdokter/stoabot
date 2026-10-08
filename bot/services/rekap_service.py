"""
Rekap service — parsing catatan forward Telegram. Keluaran memakai format buku kas seragam
reconbot (lihat bot/services/kas_report.py).
"""
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from bot.utils.formatters import parse_amount

COLUMNS = [
    "No", "Tanggal", "No. Bukti", "Uraian", "Kategori",
    "Sumber", "Debit (Masuk)", "Kredit (Keluar)", "Saldo",
]

MASUK_KEYWORDS = (
    "terima", "diterima", "masuk", "setoran", "setor", "penjualan", "jual",
    "omzet", "pendapatan", "pemasukan", "dp", "pelunasan",
)
KATEGORI_KEYWORDS = {
    "Bahan Baku": ("kopi", "susu", "gula", "es batu", "sirup", "bahan", "beras", "telur", "roti", "teh"),
    "Operasional": ("listrik", "air", "wifi", "internet", "pulsa", "gas", "token", "sewa"),
    "Gaji": ("gaji", "upah", "bonus", "lembur"),
    "Peralatan": ("alat", "gelas", "cup", "mesin", "perbaikan", "servis"),
    "Penjualan": ("penjualan", "omzet", "jual"),
}

_AMOUNT_RE = re.compile(
    r"(?:rp\.?\s*)?(\d[\d.,]*\s*(?:juta|jt|ribu|rb)?)(?![\w/])", re.IGNORECASE
)
_DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")


@dataclass
class RekapEntry:
    tanggal: date
    type: str  # masuk | keluar
    amount: float
    uraian: str
    kategori: str = "Umum"
    sumber: str = ""
    file_id: Optional[str] = None
    ocr_raw_text: Optional[str] = None
    ocr_confidence: Optional[float] = None


def guess_type(text: str, sign: str = "") -> str:
    if sign == "+":
        return "masuk"
    if sign == "-":
        return "keluar"
    words = set(re.findall(r"[a-z]+", text.lower()))
    return "masuk" if words & set(MASUK_KEYWORDS) else "keluar"


def guess_category(text: str, tx_type: str) -> str:
    low = text.lower()
    for kategori, kws in KATEGORI_KEYWORDS.items():
        if kategori == "Penjualan" and tx_type != "masuk":
            continue
        if any(re.search(rf"\b{re.escape(k)}\b", low) for k in kws):
            return kategori
    return "Penjualan" if tx_type == "masuk" else "Umum"


def _extract_date(line: str, default: date) -> tuple[str, date]:
    m = _DATE_RE.search(line)
    if not m:
        return line, default
    d, mo = int(m.group(1)), int(m.group(2))
    y = m.group(3)
    year = default.year if not y else (int(y) + 2000 if len(y) == 2 else int(y))
    try:
        found = date(year, mo, d)
    except ValueError:
        return line, default
    return (line[:m.start()] + line[m.end():]).strip(), found


def parse_note_text(text: str, default_date: date, sumber: str = "") -> tuple[list[RekapEntry], list[str]]:
    """Parse teks catatan: satu baris = satu transaksi. Return (entries, baris_diabaikan)."""
    entries: list[RekapEntry] = []
    skipped: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        line, tgl = _extract_date(line, default_date)
        sign = ""
        if line[:1] in "+-" and len(line) > 1:
            sign, line = line[0], line[1:].strip()

        matches = list(_AMOUNT_RE.finditer(line))
        amount = None
        chosen = None
        for m in reversed(matches):
            val = parse_amount(m.group(1))
            if val and val > 0:
                amount, chosen = val, m
                break
        if amount is None:
            skipped.append(raw.strip())
            continue

        uraian = (line[:chosen.start()] + " " + line[chosen.end():])
        uraian = re.sub(r"\brp\.?\b", "", uraian, flags=re.IGNORECASE)
        uraian = re.sub(r"[\s:=\-–]+$|^[\s:=\-–]+", "", re.sub(r"\s+", " ", uraian)).strip()
        if not uraian:
            skipped.append(raw.strip())
            continue
        tx_type = guess_type(uraian, sign)
        entries.append(RekapEntry(
            tanggal=tgl, type=tx_type, amount=amount, uraian=uraian,
            kategori=guess_category(uraian, tx_type), sumber=sumber,
        ))
    return entries, skipped
